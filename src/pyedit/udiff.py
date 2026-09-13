"""Unified diff support.

Parsing is pyedit's own: unidiff rejects git-canonical sections it
should accept (a no-newline create followed by anything, a delete
next to a ``diff --git`` header), and the stored-id replay path
parses pyedit's own output. Application stages every file into the
edit session overlay, so a unified diff is a dry-run like any other
input. Matching is newline-tolerant with a whitespace-insensitive
fallback pass, mirroring the fuzz behaviour of the V4A applier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class UnifiedDiffError(ValueError):
    pass


@dataclass
class AppliedFile:
    path: str
    action: str


@dataclass
class _Line:
    line_type: str
    value: str

    def __str__(self) -> str:
        return f"{self.line_type}{self.value}"


@dataclass
class _Hunk:
    source_start: int
    source_length: int
    target_start: int
    target_length: int
    lines: list[_Line] = field(default_factory=list)
    source_seen: int = 0
    target_seen: int = 0

    def __iter__(self):
        return iter(self.lines)

    def feed(self, line: _Line) -> bool:
        """Consume one hunk line; True when the hunk is complete."""
        self.lines.append(line)
        if line.line_type == " ":
            self.source_seen += 1
            self.target_seen += 1
        elif line.line_type == "+":
            self.target_seen += 1
        elif line.line_type == "-":
            self.source_seen += 1
        return (
            self.source_seen >= self.source_length
            and self.target_seen >= self.target_length
        )


@dataclass
class _PatchedFile:
    source_file: str | None = None
    target_file: str | None = None
    is_rename: bool = False
    hunks: list[_Hunk] = field(default_factory=list)
    # the verbatim "diff --git" section, kept because libgit2 applies
    # the sections pyedit has no format for; None without such a header
    section: list[str] | None = None
    binary: bool = False

    @property
    def patch_text(self) -> str:
        return "".join(self.section or ())

    @property
    def needs_git(self) -> bool:
        """True when libgit2 must apply this section, not pyedit.

        A binary payload has no text form, and a symlink's content is
        its target behind a 120000 mode that pyedit's hunks cannot
        carry.
        """
        if self.binary:
            return True
        return any(
            line.startswith(_MODE_PREFIXES) and line.rstrip().endswith("120000")
            for line in self.section or ()
        )

    @property
    def is_added_file(self) -> bool:
        return self.source_file == "/dev/null"

    @property
    def is_removed_file(self) -> bool:
        return self.target_file == "/dev/null"

    def __iter__(self):
        return iter(self.hunks)


_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)

_GIT_HEADER_RE = re.compile(r"^diff --git (\S+) (\S+)\s*$")

_MODE_PREFIXES = (
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "index ",
)


def _parse(text: str) -> list[_PatchedFile]:
    """Parse pyedit-style unified diffs; raises UnifiedDiffError.

    Line values keep their newline: the hunk consumers expect the
    git convention. The ``\\ No newline`` marker attaches to the
    open hunk, or to the last closed one when the counts already
    consumed every line."""
    files: list[_PatchedFile] = []
    lines = text.splitlines(keepends=True)
    current: _PatchedFile | None = None
    hunk: _Hunk | None = None
    expect = None
    section: list[str] | None = None
    header: str | None = None
    binary = False
    for raw in lines:
        # the section is captured before anything consumes the line:
        # a hunk body belongs to it too, and libgit2 rejects a section
        # whose hunks are missing
        if raw.startswith("diff --git "):
            section = [raw]
            header = raw
            binary = False
        elif section is not None:
            section.append(raw)
        if binary:
            # base85 payload lines; the whole section goes to libgit2
            continue
        if hunk is not None:
            if raw.startswith("\\"):
                hunk.lines.append(_Line("\\", raw[1:]))
                continue
            if raw[:1] in (" ", "+", "-"):
                if hunk.feed(_Line(raw[0], raw[1:])):
                    hunk = None
                continue
            raise UnifiedDiffError(f"cannot parse hunk line: {raw!r}")
        if raw.startswith("GIT binary patch"):
            current = _binary_file(header, section)
            files.append(current)
            binary = True
            continue
        if raw.startswith("\\"):
            if files and files[-1].hunks:
                files[-1].hunks[-1].lines.append(_Line("\\", raw[1:]))
            continue
        if raw.startswith("--- "):
            source = raw[4:].rstrip("\n").split("\t")[0]
            same = (
                current is not None
                and current.target_file is not None
                and _strip_prefix(source) == current.source_file
            )
            if not same:
                current = _PatchedFile(source_file=source, section=section)
                files.append(current)
            expect = "target"
            continue
        if raw.startswith("+++ "):
            if current is None:
                raise UnifiedDiffError(
                    f"target without source: {raw.rstrip()}"
                )
            current.target_file = raw[4:].rstrip("\n").split("\t")[0]
            expect = None
            continue
        if raw.startswith("rename from "):
            if current is None:
                current = _PatchedFile()
                files.append(current)
            current.source_file = raw[12:].rstrip("\n")
            continue
        if raw.startswith("rename to "):
            if current is None:
                current = _PatchedFile()
                files.append(current)
            current.target_file = raw[10:].rstrip("\n")
            current.is_rename = True
            continue
        m = _HUNK_RE.match(raw)
        if m and current is not None:
            hunk = _Hunk(
                int(m.group(1)),
                int(m.group(2) or "1"),
                int(m.group(3)),
                int(m.group(4) or "1"),
            )
            current.hunks.append(hunk)
            continue
        if raw.startswith(
            (
                "#",
                "diff --git",
                "index ",
                "similarity ",
                "new file mode",
                "deleted file mode",
                "old mode",
                "new mode",
                "copy from",
                "copy to",
                "Binary file",
                "Symlink ",
            )
        ):
            continue
        if raw.strip() == "":
            continue
        raise UnifiedDiffError(f"cannot parse line: {raw!r}")
    for f in files:
        if f.source_file is None and f.target_file is None:
            raise UnifiedDiffError("file section without source and target")
    return files


def _binary_file(header: str | None, section: list[str]) -> _PatchedFile:
    """A binary section's paths come from its ``diff --git`` header.

    A binary patch carries no ``---``/``+++`` pair, so the header is
    the only source of the paths.
    """
    if header is None:
        raise UnifiedDiffError("binary patch without a 'diff --git' header")
    match = _GIT_HEADER_RE.match(header)
    if not match:
        raise UnifiedDiffError(
            f"cannot read the paths of a binary patch from: {header.rstrip()}"
        )
    return _PatchedFile(
        source_file=match.group(1),
        target_file=match.group(2),
        section=section,
        binary=True,
    )


def apply_diff(session, text: str, strict: bool = True) -> list[AppliedFile]:
    """Parse a unified diff and stage every file on the session.

    Fails closed: a file whose hunks do not apply raises and rolls
    the whole input back, so a caught error never leaves part of
    the patch staged. strict=False skips the failed files (the
    caller warns) and stages the rest."""
    try:
        patch_set = _parse(text)
    except UnifiedDiffError:
        raise
    except Exception as err:
        raise UnifiedDiffError(f"invalid unified diff: {err}") from err
    applied: list[AppliedFile] = []
    failures: list[str] = []
    mark = session.checkpoint()
    for patched in patch_set:
        file_mark = session.checkpoint()
        try:
            applied.append(_apply_file(session, patched))
        except Exception:
            session.rollback(file_mark)
            if strict:
                session.rollback(mark)
                raise
            failures.append(_strip_prefix(patched.target_file)
                            or _strip_prefix(patched.source_file)
                            or "?")
    return applied, failures


def _apply_file(session, patched: PatchedFile) -> AppliedFile:
    source = _strip_prefix(patched.source_file)
    target = _strip_prefix(patched.target_file)
    hunks = list(patched)

    if patched.needs_git:
        return _apply_through_git(session, patched, source, target)

    if patched.is_removed_file:
        session.delete(source)
        return AppliedFile(path=source, action="deleted")

    if patched.is_added_file or source is None:
        if target is None:
            raise UnifiedDiffError("patch adds a file without a target path")
        session.write(target, _assemble_new(hunks))
        return AppliedFile(path=target, action="created")

    if source != target and not hunks:
        session.rename(source, target)
        return AppliedFile(path=target, action="renamed")

    if not hunks:
        raise UnifiedDiffError(f"{target}: no hunks and no binary payload")

    content = session.read(source)
    if isinstance(content, bytes):
        raise UnifiedDiffError(f"{source} is binary; unified diffs apply to text only")
    patched_content = _apply_hunks(content, hunks)
    if source != target:
        session.rename(source, target)
        session.write(target, patched_content)
    else:
        session.write(source, patched_content)
    return AppliedFile(path=target, action="updated")


_LINK_MODE = 0o120000


def _apply_through_git(session, patched, source: str, target: str) -> AppliedFile:
    """Stage a section that only libgit2 can read.

    A binary payload and a symlink's 120000 mode have no form in
    pyedit's own hunks, so the whole section goes to `pyedit.memgit`,
    which applies it to a one-file tree holding what the session has
    now. libgit2 verifies a binary patch by reversing it back onto the
    preimage, so a payload that does not belong to this file fails
    here rather than corrupting it.
    """
    from pyedit.memgit import MemGitError, MemoryRepo

    # a create says "--- /dev/null", so there is no source to seed from
    before = {}
    if source is not None:
        existing = _session_entry(session, source)
        if existing is not None:
            before[source] = existing

    repo = MemoryRepo()
    try:
        result = repo.apply(repo.tree(before), patched.patch_text)
    except MemGitError as err:
        raise UnifiedDiffError(f"{target or source}: {err}") from err

    for path, entry in result.items():
        if entry.mode == _LINK_MODE:
            _stage_link(session, path, entry.data.decode())
        else:
            session.write(path, entry.data)
    if source is not None and source not in result:
        session.delete(source)
        if not result:
            return AppliedFile(path=source, action="deleted")
        return AppliedFile(path=target, action="renamed")
    return AppliedFile(path=target, action="created" if not before else "updated")


def _session_entry(session, path):
    """What the session holds for `path`, as memgit's (content, mode).

    None when nothing is there: the patch creates the file.
    """
    import pygit2

    from pyedit.session import Symlink

    # staged state wins over the disk: a script may have written text
    # onto a path that is still a symlink out there
    staged = session.staged().get(session.canon(path), _UNSET)
    if staged is None:
        return None
    if isinstance(staged, Symlink):
        return (str(staged), pygit2.enums.FileMode.LINK)
    if staged is not _UNSET:
        return (staged, pygit2.enums.FileMode.BLOB)
    target = _disk_link_target(path)
    if target is not None:
        return (target, pygit2.enums.FileMode.LINK)
    try:
        return (session.read(path), pygit2.enums.FileMode.BLOB)
    except FileNotFoundError:
        return None


_UNSET = object()


def _disk_link_target(path) -> str | None:
    from pyedit.session import _disk_is_link, _disk_readlink

    from pathlib import Path

    p = Path(path)
    return _disk_readlink(p) if _disk_is_link(p) else None


def _stage_link(session, path, target: str) -> None:
    session.symlink(target, path, force=True)


def _strip_prefix(path: str) -> str | None:
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _assemble_new(hunks) -> str:
    text = ""
    new_no_nl = False
    for hunk in hunks:
        _, new_block, _context, marker = _split_hunk(hunk)
        new_no_nl = new_no_nl or marker
        text += "".join(new_block)
    if new_no_nl and text.endswith("\n"):
        text = text[:-1]
    return text


def _apply_hunks(content: str, hunks) -> str:
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    ends_without_newline = False

    for hunk in hunks:
        old_block, new_block, context, new_no_nl = _split_hunk(hunk)

        hint = hunk.source_start - 1 if hunk.source_length else hunk.source_start
        index = _find_block(lines, old_block, hint)
        if index < cursor:
            raise UnifiedDiffError(f"overlapping hunks around line {hint + 1}")
        if new_no_nl and index + len(old_block) >= len(lines):
            ends_without_newline = True
        result.extend(lines[cursor:index])
        # context lines come from the file, not from the patch: the
        # match may have come from the whitespace-insensitive pass,
        # and the file's own spacing is the one that survives
        block = list(new_block)
        for new_at, old_at in context:
            block[new_at] = lines[index + old_at]
        result.extend(block)
        cursor = index + len(old_block)

    result.extend(lines[cursor:])
    body = []
    for line in result[:-1]:
        body.append(line if line.endswith("\n") else line + "\n")
    if result:
        body.append(result[-1])
    if ends_without_newline and body and body[-1].endswith("\n"):
        body[-1] = body[-1][:-1]
    return "".join(body)


def _split_hunk(hunk) -> tuple[list[str], list[str], list[tuple[int, int]], bool]:
    """Split hunk lines into (old block, new block, context map, new side
    ends without newline).

    The context map pairs each context line's index in the new block
    with its index in the old block, so a caller can substitute the
    file's own line for the patch's copy.

    A '\\ No newline at end of file' marker annotates the '-' or '+' line
    right before it; only the '+' side matters when assembling.
    """
    old_block: list[str] = []
    new_block: list[str] = []
    context: list[tuple[int, int]] = []
    pending: str | None = None
    new_no_nl = False
    for line in hunk:
        line_type = line.line_type
        if line_type == "\\":
            if pending == "new":
                new_no_nl = True
            pending = None
        elif line_type == "+":
            new_block.append(str(line)[1:])
            pending = "new"
        else:
            old_block.append(str(line)[1:])
            if line_type == " ":
                context.append((len(new_block), len(old_block) - 1))
                new_block.append(str(line)[1:])
            pending = None if line_type == " " else "old"
    return old_block, new_block, context, new_no_nl


def _find_block(lines: list[str], block: list[str], hint: int) -> int:
    if not block:
        return min(hint, len(lines))
    stripped = [line.rstrip("\n") for line in lines]
    target = [line.rstrip("\n") for line in block]
    for compare in (lambda a, b: a == b, lambda a, b: a.rstrip() == b.rstrip()):
        for index in _search_order(len(lines), len(block), hint):
            if all(
                compare(stripped[index + offset], target[offset])
                for offset in range(len(block))
            ):
                return index
    raise UnifiedDiffError(f"patch context not found near line {hint + 1}")


def _search_order(length: int, size: int, hint: int):
    # hint, hint-1, hint+1, hint-2, hint+2 ... Walk to both edges:
    # counting candidates instead spends the count on positions
    # outside the file and gives up early on a hint near an edge
    last = length - size
    if last < 0:
        return
    hint = max(0, min(hint, last))
    yield hint
    for offset in range(1, last + 1):
        if hint - offset >= 0:
            yield hint - offset
        if hint + offset <= last:
            yield hint + offset
