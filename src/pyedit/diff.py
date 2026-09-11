"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), so the output can be piped to ``git apply``
or ``patch -p1``.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pyedit.session import display_path


def unified_diffs(
    staged: dict[Path, str | None], context: int = 3
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change with a diff."""
    results: list[tuple[str, str]] = []
    for path, new in sorted(staged.items()):
        old = path.read_text() if path.is_file() else None
        if new is None and old is None:
            continue
        rel = display_path(path)
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
