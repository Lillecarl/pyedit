"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), so text output can be piped to ``git
apply`` or ``patch -p1``. Changes involving bytes render as one-line
summaries instead of hunks.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pyedit.session import _disk_is_file, _slurp, display_path


def original(path: Path) -> str | bytes | None:
    if not _disk_is_file(path):
        return None
    return _slurp(path)


def unified_diffs(
    staged: dict[Path, str | bytes | None], context: int = 3
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change."""
    results: list[tuple[str, str]] = []
    for path, new in sorted(staged.items()):
        old = original(path)
        if new is None and old is None:
            continue
        rel = display_path(path)
        if isinstance(new, bytes) or isinstance(old, bytes):
            results.append((rel, binary_note(rel, old, new)))
            continue
        a_lines = old.splitlines(keepends=True) if old is not None else []
        b_lines = new.splitlines(keepends=True) if new is not None else []
        fromfile = f"a/{rel}" if old is not None else "/dev/null"
        tofile = f"b/{rel}" if new is not None else "/dev/null"
        diff = "".join(
            difflib.unified_diff(
                a_lines, b_lines, fromfile=fromfile, tofile=tofile, n=context
            )
        )
        if diff:
            results.append((rel, diff))
    return results


def binary_note(rel: str, old: str | bytes | None, new: str | bytes | None) -> str:
    if new is None:
        return f"Binary file {rel} deleted ({len(old)} bytes)\n"
    if old is None:
        return f"Binary file {rel} created ({len(new)} bytes)\n"
    return f"Binary file {rel} changed ({len(old)} -> {len(new)} bytes)\n"
