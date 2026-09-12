"""Unified diff support.

Parsing uses ``unidiff``; application stages every file into the edit
session overlay, so a unified diff is a dry-run like any other input.
Matching is newline-tolerant (files may or may not end in a newline)
with a whitespace-insensitive fallback pass, mirroring the fuzz
behaviour of the V4A applier.
"""

from __future__ import annotations

from dataclasses import dataclass

from unidiff import PatchSet, PatchedFile


class UnifiedDiffError(ValueError):
    pass


@dataclass
class AppliedFile:
    path: str
    action: str


def apply_diff(session, text: str) -> list[AppliedFile]:
    """Parse a unified diff and stage every file on the session."""
    try:
        patch_set = PatchSet.from_string(text)
    except Exception as err:
        raise UnifiedDiffError(f"invalid unified diff: {err}") from err
    applied: list[AppliedFile] = []
    for patched in patch_set:
        applied.append(_apply_file(session, patched))
    return applied


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
