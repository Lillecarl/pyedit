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

from pathlib import Path

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
                "no edit session is running: VFS scopes merge into the active session"
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


def commit(message: str, *, after: str = "@-"):
    """A scope whose tree becomes a commit after `after`.

    with pyedit.commit("split out the rename", after="abc123"):
        pyedit.edit("a.py", old, new)

    On clean exit the scope's tree lands as a new commit on top of
    `after` (a revset naming one commit, default `@-`) and `after`'s
    children reparent onto it, so the stack keeps its shape with the
    new commit in the middle. Naming the working-copy commit itself
    refuses: describe it and advance instead. On error nothing is
    written anywhere. The scope edits `after`'s tree, not
    the working copy, and its tree is not staged on the parent: the
    content lives in history. An empty scope refuses instead of
    making an empty commit; describe()/author() inside refuse naming
    the message argument, since they stage metadata for -r runs.
    """
    return CommitScope(message, after)


class CommitScope:
    """The open-commit behind `pyedit.commit()`."""

    def __init__(self, message: str, after: str = "@-") -> None:
        if not isinstance(message, str):
            raise TypeError(f"commit() message needs str, got {type(message).__name__}")
        if not isinstance(after, str):
            raise TypeError(f"commit() revision needs str, got {type(after).__name__}")
        self._message = message
        self._after = after
        self._session = None
        self._parent = None
        self._repo_root = None
        self._base_hex = None
        self._tmp = None

    def __enter__(self) -> EditSession:
        import tempfile

        import pyedit
        from pyedit import jjrev
        from pyedit.active import current, push

        self._parent = current() or getattr(pyedit, "session", None)
        if self._parent is None:
            raise RuntimeError(
                "no edit session is running: commit scopes belong to the active session"
            )
        self._repo_root = jjrev.find_repo_root(self._parent.root)
        base_hex, _wc_hex = jjrev.resolve_ids(self._repo_root, self._after)
        self._base_hex = base_hex
        from pyedit import vfs

        # the base tree materializes on disk: under the installed
        # overlay these calls would stage into the parent session
        # instead of reaching the scratch dir
        with vfs.suspended():
            self._tmp = tempfile.TemporaryDirectory(prefix="pyedit-commit")
            jjrev.export_tree(self._repo_root, base_hex, Path(self._tmp.name))
            self._session = EditSession(
                root=Path(self._tmp.name),
                max_bytes=self._parent._max_bytes,
                max_files=self._parent._max_files,
                respect_gitignore=self._parent._respect_gitignore,
                find_renames=self._parent._find_renames,
            )
        push(self._session)
        return self._session

    def __exit__(self, exc_type, exc, tb) -> bool:
        from pyedit import jjrev, vfs
        from pyedit.active import pop

        scope = pop()
        # prune reads the scratch tree, the amend borrows
        # working-copy files, cleanup removes the scratch dir: under
        # the installed overlay each would stage into the parent
        # instead of reaching disk
        with vfs.suspended():
            try:
                if exc_type is not None:
                    return False
                if scope.meta:
                    raise jjrev.JjRevError(
                        "describe()/author() stage metadata for -r runs; "
                        "the new commit's message comes from "
                        "pyedit.commit(...)"
                    )
                scope.prune_unchanged()
                changes = {
                    path.relative_to(scope.root).as_posix(): content
                    for path, content in scope.staged().items()
                }
                new_hex, op, cleanup_op, conflicts = jjrev.create_after(
                    self._repo_root, self._base_hex, self._message, changes
                )
            finally:
                self._tmp.cleanup()
        subject = self._message.splitlines()
        self._parent._history.extend(scope.history)
        self._parent._history.append(
            {
                "id": new_hex,
                "subject": subject[0] if subject else "(no description)",
                "op": op,
                "cleanup_op": cleanup_op,
                "conflicts": list(conflicts),
            }
        )
        return False
