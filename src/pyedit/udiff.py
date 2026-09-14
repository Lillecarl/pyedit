"""Unified diffs, read by the unidiff library.

`unidiff` parses; pyedit applies. The library has no applier at all,
and pyedit's is the reason this path exists: it anchors a hunk by
searching outward from the line number, with a whitespace-insensitive
second pass, so an agent's approximate `@@` numbers still land. Use
`pyedit.apply_diff_git` when the numbers are exact and the patch is
git-canonical -- that one is libgit2 end to end and handles binary
payloads, symlinks and modes, which this one cannot.

Binary sections raise here rather than routing away quietly: one
format, one applier, and the error names the other entry point.
"""

from __future__ import annotations

from dataclasses import dataclass


_LINK_MODE = "120000"


class UnifiedDiffError(ValueError):
    pass


@dataclass
class AppliedFile:
    path: str
    action: str


def _parse(text: str):
    """unidiff's PatchedFile objects, which carry what pyedit needs.

    Its Line has `line_type` and `value` with the newline kept, and a
    `\\ No newline` marker arrives as a line of type "\\", which is
    exactly what the hunk consumers below expect.

    Each file section is handed over on its own. unidiff 1.0.0 has two
    faults that only show up in a multi-file diff, and both disappear
    when a section is parsed alone:

    - two consecutive `--- /dev/null` creates with no `diff --git`
      header raise "Target without source" on the second, and that is
      a shape agents write constantly;
    - when a `diff --git` header names paths that the `---` line
      disagrees with -- which every create does, since its source is
      `/dev/null` -- it yields a second, empty PatchedFile.

    Finding the section boundaries is not diff parsing; every hunk and
    every line still comes from the library.
    """
    import unidiff

    files = []
    for section in _sections(text):
        try:
            patch = unidiff.PatchSet(section)
        except unidiff.UnidiffParseError as err:
            raise UnifiedDiffError(f"cannot read this diff: {err}") from err
        for patched in patch:
            if patched.is_binary_file:
                raise UnifiedDiffError(
                    f"{patched.path}: a binary section needs apply_diff_git; "
                    "unidiff reads text hunks only"
                )
            # a symlink is a text hunk holding the target, and only
            # the 120000 mode says so. Applying it here would write a
            # regular file with the target as its content.
            if _LINK_MODE in (
                str(patched.source_mode or ""),
                str(patched.target_mode or ""),
            ):
                raise UnifiedDiffError(
                    f"{patched.path}: a symlink section needs apply_diff_git; "
                    "a 120000 mode is not something a text hunk can carry"
                )
            files.append(patched)
    return files


def _sections(text: str) -> list[str]:
    """Split a diff at its file boundaries.

    A boundary is a `diff --git` line, or a `--- ` line followed by a
    `+++ ` line when no `diff --git` opened the current section. A
    removed line "--- x" directly above an added "+++ y" reads as a
    boundary here, as it does in any headerless diff.
    """
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    header_seen = False
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            starts.append(index)
            header_seen = True
        elif (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            if not header_seen:
                starts.append(index)
            header_seen = False
    if not starts:
        return [text] if text.strip() else []
    # a preamble before the first section (pyedit prints id comments
    # around its diffs) rides along with it; unidiff ignores it
    starts[0] = 0
    bounds = starts + [len(lines)]
    return [
        "".join(lines[a:b]) for a, b in zip(starts, bounds[1:]) if "".join(lines[a:b]).strip()
    ]


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


def _apply_file(session, patched) -> AppliedFile:
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
