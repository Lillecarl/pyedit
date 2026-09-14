"""Independent edit scopes with contextual merging.

You are always editing inside a root VFS -- the session. ``with
pyedit.VFS():`` opens an independent scope: inside the with-body,
every ``pyedit.*`` call edits a fresh overlay over disk truth (the
scope never sees the parent's staged state or sibling scopes); when
the body ends, git merges the scope onto the parent -- disk truth is
the ancestor, the parent is ours, the scope is theirs. Scopes nest;
each merges into the scope it was opened in. `gitmerge.py` does the
merge.

Merging fails loudly instead of guessing. ``Collision`` is raised when
two scopes change the same region, when one deletes what another
changes, when both create a path with different content, or when a
binary file diverges. git merges two changed regions only if an
unchanged line separates them, so two edits to neighbouring lines
collide and belong in one scope. A scope whose body raises is
discarded whole: nothing of it merges, the parent is untouched.
"""

from __future__ import annotations

from pyedit.session import EditSession


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
            find_renames=self._parent._find_renames,
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
        from pyedit import gitmerge

        gitmerge.merge(self._parent, child)
