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
    for raw in lines:
        if hunk is not None:
            if raw.startswith("\\"):
                hunk.lines.append(_Line("\\", raw[1:]))
                continue
            if raw[:1] in (" ", "+", "-"):
                if hunk.feed(_Line(raw[0], raw[1:])):
                    hunk = None
                continue
            raise UnifiedDiffError(f"cannot parse hunk line: {raw!r}")
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
                current = _PatchedFile(source_file=source)
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
        raise UnifiedDiffError(f"{target}: binary patches are not supported")

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
        _, new_block, marker = _split_hunk(hunk)
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
        old_block, new_block, new_no_nl = _split_hunk(hunk)

        hint = hunk.source_start - 1 if hunk.source_length else hunk.source_start
        index = _find_block(lines, old_block, hint)
        if index < cursor:
            raise UnifiedDiffError(f"overlapping hunks around line {hint + 1}")
        if new_no_nl and index + len(old_block) >= len(lines):
            ends_without_newline = True
        result.extend(lines[cursor:index])
        result.extend(new_block)
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


def _split_hunk(hunk) -> tuple[list[str], list[str], bool]:
    """Split hunk lines into (old block, new block, new side ends without newline).

    A '\\ No newline at end of file' marker annotates the '-' or '+' line
    right before it; only the '+' side matters when assembling.
    """
    old_block: list[str] = []
    new_block: list[str] = []
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
                new_block.append(str(line)[1:])
            pending = None if line_type == " " else "old"
    return old_block, new_block, new_no_nl


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
    # hint, hint-1, hint+1, hint-2, hint+2 ...
    for offset in range(length - size + 1):
        if offset % 2 == 0:
            position = hint + offset // 2
        else:
            position = hint - (offset + 1) // 2
        if 0 <= position <= length - size:
            yield position
