"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), so text output can be piped to ``git
apply`` or ``patch -p1``. Hunk bodies come from ``pyedit.render``
(libgit2); this module handles per-file orchestration and headers.
Changes involving bytes render as one-line summaries instead of hunks.
"""

from __future__ import annotations

from pathlib import Path

from pyedit.render import DiffRenderer
from pyedit.session import _disk_is_file, _slurp, display_path

# one process-wide renderer; it imports pygit2 lazily, so module import
# stays cheap and cert-less environments fail at render time only
_RENDERER = DiffRenderer()


def original(path: Path) -> str | bytes | None:
    if not _disk_is_file(path):
        return None
    return _slurp(path)


def unified_diffs(
    staged: dict[Path, str | bytes | None],
    context: int = 3,
    base: dict[Path, str | bytes | None] | None = None,
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change.

    The old side comes from `base` when given (a value of None means
    the file was not there), otherwise from disk truth. Reversing a
    diff means swapping the maps: undo = unified_diffs(pre_apply,
    base=post_apply).
    """
    results: list[tuple[str, str]] = []
    for path, new in sorted(staged.items()):
        old = base.get(path) if base is not None else original(path)
        if new is None and old is None:
            continue
        rel = display_path(path)
        if isinstance(new, bytes) or isinstance(old, bytes):
            if new == old:
                # an identical binary stage is not a change
                continue
            results.append((rel, binary_note(rel, old, new)))
            continue
        fromfile = f"a/{rel}" if old is not None else "/dev/null"
        tofile = f"b/{rel}" if new is not None else "/dev/null"
        old_text = "" if old is None else old
        new_text = "" if new is None else new
        hunks = _RENDERER.hunks(old_text, new_text, context)
        if not hunks:
            if old is None or new is None:
                # an empty file created or deleted renders as headers
                # alone; skipping it would hide the change entirely
                results.append((rel, f"--- {fromfile}\n+++ {tofile}\n"))
            continue
        results.append((rel, f"--- {fromfile}\n+++ {tofile}\n" + hunks))
    return results


def binary_note(rel: str, old: str | bytes | None, new: str | bytes | None) -> str:
    if new is None:
        return f"Binary file {rel} deleted ({len(old)} bytes)\n"
    if old is None:
        return f"Binary file {rel} created ({len(new)} bytes)\n"
    return f"Binary file {rel} changed ({len(old)} -> {len(new)} bytes)\n"
