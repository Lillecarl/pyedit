"""Independent edit scopes with contextual merging.

Each ``VFS`` scope stages its edits in a fresh overlay over disk truth.
When a child scope (``VFS(parent)``) exits cleanly, its changes render
as hunks anchored by context alone — line numbers are advisory and
ignored — and merge onto the parent's state. Sibling scopes never see
each other's edits while they run, and an edit does not have to
account for lines moved by an earlier one: its hunks re-anchor on the
merged text.

Merging fails loudly instead of guessing. ``Collision`` is raised when
a hunk's context matches no line (the region changed incompatibly), is
ambiguous (matches several places), folds back over an earlier hunk of
the same patch, a file was deleted/created/modified by two scopes with
different results, or a binary file diverged. A failed merge leaves
the parent untouched, so nothing partial survives; a collector whose
body raises discards every merge it absorbed.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from pyedit.session import EditSession, _MISSING, _disk_is_file, _slurp
from pyedit.vfs import install

CONTEXT = 3

# captured at import time, before any VFS install: apply() must write
# the real disk even while called from inside a patched scope body
_REAL_MAKEDIRS = os.makedirs
_REAL_REMOVE = os.remove
_REAL_OPEN = os.open
_REAL_WRITE = os.write
_REAL_CLOSE = os.close


class Collision(ValueError):
    """Two edits disagree and the merge refuses to guess."""


class VFS:
    """An edit scope.

    ``VFS()`` creates a collector: it accumulates the merges of the
    child scopes created with ``VFS(collector)`` and, after its own
    ``with`` body, holds the merged session. Each child is a fresh
    overlay over disk truth; on clean exit it merges into the
    collector, on error nothing of it survives. Attribute access
    forwards to the scope's session, so a scope is used exactly like
    the ``pyedit`` global inside an edit script.
    """

    def __init__(self, parent: "VFS | None" = None) -> None:
        if parent is not None and not isinstance(parent, VFS):
            raise TypeError("VFS(parent) takes another VFS instance")
        self._parent = parent
        self._session = EditSession()
        self._restore = None

    @property
    def session(self) -> EditSession:
        return self._session

    def __enter__(self) -> "VFS":
        self._restore = install(self._session)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        restore, self._restore = self._restore, None
        restore()
        if exc_type is None:
            if self._parent is not None:
                self._parent._merge(self._session)
        elif self._parent is None:
            # a failed collector is a failed transaction
            self._session._staged.clear()
            self._session._bytes_used = 0
            self._session._files_used = 0
        return False

    # --- collector side ---

    def _merge(self, child: EditSession) -> None:
        child.prune_unchanged()
        results: list[tuple[Path, str | bytes | None]] = []
        for path, content in sorted(child.staged().items()):
            results.append((path, _merge_one(self._session, path, content)))
        for path, content in results:
            self._session._stage(path, content)

    # --- merged-state views ---

    def staged(self) -> dict[Path, str | bytes | None]:
        return self._session.staged()

    def staged_content(self, path) -> str | bytes | None:
        return self._session.staged_content(path)

    def diff(self, context: int = CONTEXT) -> str:
        from pyedit.diff import unified_diffs

        return "".join(d for _, d in unified_diffs(self._session.staged(), context))

    def apply(self) -> None:
        """Write the merged state to disk (raw os calls, so this is safe
        inside a scope whose filesystem patches are still installed)."""
        for path, content in sorted(self._session.staged().items()):
            try:
                if content is None:
                    _REAL_REMOVE(path)
                    continue
                _REAL_MAKEDIRS(path.parent, exist_ok=True)
                if isinstance(content, bytes):
                    payload = content
                else:
                    payload = content.encode("utf-8")
                fd = _REAL_OPEN(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                try:
                    _REAL_WRITE(fd, payload)
                finally:
                    _REAL_CLOSE(fd)
            except IsADirectoryError:
                raise Collision(f"{path}: cannot overwrite a directory") from None
            except FileNotFoundError:
                raise Collision(f"{path}: parent directory vanished") from None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._session, name)


def _merge_one(
    collector: EditSession, path: Path, content: str | bytes | None
) -> str | bytes | None:
    """Merge one staged path into the collector; returns merged content."""
    base = _base_content(path)
    staged = collector.staged_content(path)
    current = staged if staged is not _MISSING else base

    if content is None:
        if staged is not None and staged is not _MISSING and not _same(current, base):
            raise Collision(f"{path}: deleted here but changed by an earlier edit")
        return None

    if not isinstance(content, str):
        if staged is None:
            raise Collision(f"{path}: binary file was deleted by an earlier edit")
        if _same(current, content) or _same(current, base):
            return content
        raise Collision(f"{path}: binary file changed by two edits")

    # text
    if staged is None:
        raise Collision(f"{path}: file was deleted by an earlier edit")
    if _same(current, content):
        return content
    if _same(current, base):
        return content
    if base is None:
        raise Collision(f"{path}: created by two edits with different content")
    return _apply_hunks(path, current, _hunks(base, content))


def _hunks(base: str, content: str) -> list[tuple[list[str], list[str], int, int]]:
    """Change hunks as (old block, new block, leading context, trailing
    context).

    The old block is what must re-anchor on the merged text; line
    numbers are deliberately not recorded. Leading and trailing counts
    say how many of the block's end lines are pure context, so
    application can drop them to re-anchor when an earlier edit moved
    or replaced nearby lines.
    """
    a = base.splitlines(keepends=True)
    b = content.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    hunks: list[tuple[list[str], list[str], int, int]] = []
    carry: list[str] = []
    old: list[str] = []
    new: list[str] = []
    lead = trail = 0
    open_ = False
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            equal = a[a1:a2]
            if open_:
                if len(equal) <= 2 * CONTEXT:
                    old += equal
                    new += equal
                    trail += len(equal)
                    continue
                old += equal[:CONTEXT]
                new += equal[:CONTEXT]
                trail += CONTEXT
                hunks.append((old, new, lead, trail))
                old, new, open_ = [], [], False
            carry = list(equal[-CONTEXT:] if len(equal) > CONTEXT else equal)
            continue
        if not open_:
            old, new = list(carry), list(carry)
            lead, trail = len(carry), 0
            open_ = True
        old += a[a1:a2]
        new += b[b1:b2]
    if open_:
        hunks.append((old, new, lead, trail))
    return hunks


def _apply_hunks(
    path: Path,
    content: str,
    hunks: list[tuple[list[str], list[str], int, int]],
) -> str:
    """Re-anchor every hunk by context on `content`; collisions abort.

    Context shrinks symmetrically (like `git apply` fuzz) until the
    block matches exactly one position at or after the previous hunk;
    if the full context matches more than one place the edit is
    ambiguous and refuses.
    """
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    for old_block, new_block, lead, trail in hunks:
        index = None
        cut_lead = cut_trail = 0
        level = 0
        limit = max(lead, trail)
        matched_anywhere = False
        while level <= limit:
            cut_lead = min(level, lead)
            cut_trail = min(level, trail)
            block = old_block[cut_lead : len(old_block) - cut_trail]
            if block:
                positions = _match_positions(lines, block)
                matched_anywhere = matched_anywhere or bool(positions)
                candidates = [i for i in positions if i >= cursor]
                if len(candidates) > 1:
                    raise Collision(
                        f"{path}: hunk context matches {len(candidates)} "
                        "places; ambiguous"
                    )
                if len(candidates) == 1:
                    index = candidates[0]
                    break
            level += 1
        if index is None:
            if matched_anywhere:
                raise Collision(
                    f"{path}: hunk context only matches before an earlier hunk"
                )
            raise Collision(f"{path}: hunk context not found in the merged text")
        replacement = new_block[cut_lead : len(new_block) - cut_trail]
        result.extend(lines[cursor:index])
        result.extend(replacement)
        cursor = index + len(block)
    result.extend(lines[cursor:])
    return "".join(result)


def _match_positions(lines: list[str], block: list[str]) -> list[int]:
    stripped = [line.rstrip("\n") for line in lines]
    target = [line.rstrip("\n") for line in block]
    exact = [
        i
        for i in range(len(lines) - len(block) + 1)
        if stripped[i : i + len(block)] == target
    ]
    if len(exact) == 1:
        return exact
    loose = [
        i
        for i in range(len(lines) - len(block) + 1)
        if [s.rstrip() for s in stripped[i : i + len(block)]]
        == [t.rstrip() for t in target]
    ]
    return loose


def _base_content(path: Path) -> str | bytes | None:
    if not _disk_is_file(path):
        return None
    return _slurp(path)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return a == b
