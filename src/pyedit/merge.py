"""Independent edit scopes with contextual merging.

You are always editing inside a root VFS -- the session. ``with
pyedit.VFS():`` opens an independent scope: inside the with-body,
every ``pyedit.*`` call edits a fresh overlay over disk truth (the
scope never sees the parent's staged state or sibling scopes); when
the body ends, the scope's changes render as hunks anchored by
context alone -- line numbers are advisory and ignored -- and merge
onto the parent. An edit does not have to account for lines moved by
an earlier one: its hunks re-anchor on the merged text. Scopes nest;
each merges into the scope it was opened in.

Merging fails loudly instead of guessing. ``Collision`` is raised when
a hunk's context matches no line (the region changed incompatibly), is
ambiguous (matches several places), folds back over an earlier hunk of
the same patch, a file was deleted/created/modified by two scopes with
different results, or a binary file diverged. A scope whose body
raises is discarded whole: nothing of it merges, the parent is
untouched.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pyedit.session import EditSession, _MISSING, _disk_is_file, _slurp


CONTEXT = 3


class Collision(ValueError):
    """Two edits disagree and the merge refuses to guess."""


class VFS:
    """An independent edit scope.

    with pyedit.VFS():
        pyedit.edit("a.py", old, new)

    On enter, a fresh overlay over disk truth becomes the active
    session: every ``pyedit.*`` call inside the with-body edits the
    scope. On clean exit the scope merges into the session it was
    opened in; on error nothing of it survives.
    """

    def __init__(self) -> None:
        self._session = None
        self._parent = None

    def __enter__(self) -> EditSession:
        import pyedit

        from pyedit.active import current, push

        # the parent is the active scope, or the root session bound on
        # the module (set by the CLI run or the test harness)
        self._parent = current() or getattr(pyedit, "session", None)
        if self._parent is None:
            raise RuntimeError(
                "no edit session is running: VFS scopes merge into the "
                "active session"
            )
        # the scope edits the parent's tree, not the cwd: its root
        # and budgets must be the parent's, or paths and limits
        # diverge the moment a library caller passes root=
        self._session = EditSession(
            root=self._parent.root,
            max_bytes=self._parent._max_bytes,
            max_files=self._parent._max_files,
            respect_gitignore=self._parent._respect_gitignore,
        )
        push(self._session)
        return self._session

    def __exit__(self, exc_type, exc, tb) -> bool:
        from pyedit.active import pop

        scope = pop()
        if exc_type is None and self._parent is not None:
            self._merge(scope)
        return False

    def _merge(self, child: EditSession) -> None:
        import os

        if os.environ.get("PYEDIT_GIT_MERGE"):
            from pyedit import gitmerge

            try:
                return gitmerge.merge(self._parent, child)
            except gitmerge.GitMergeError as err:
                raise Collision(str(err)) from err
        child.prune_unchanged()
        results: list[tuple[Path, str | bytes | None]] = []
        for path, content in sorted(child.staged().items()):
            results.append((path, _merge_one(self._parent, path, content)))
        for path, content in results:
            self._parent._stage(path, content)


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
