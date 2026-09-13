"""Apply a git-canonical patch to the session, through libgit2.

This is the inverse of `pyedit.diff.replayable_patch`, and the two are
the whole of pyedit's internal patch loop: what pyedit generates for a
dry-run id or an undo id comes back in here, and libgit2 does both
halves. No Python parses a diff on this path.

That works because the patch is git's own: `diff --git` headers, mode
lines, base85 payloads for bytes, 120000 entries for symlinks. libgit2
refuses anything looser -- a headerless section, a mode-less create --
which is exactly why a patch written by something else does not come
here.

Line numbers are exact by construction: the patch was rendered from the
state it is applied to. libgit2 checks one position per hunk and does
not search, and here it does not need to.
"""

from __future__ import annotations

from pyedit.memgit import MemGitError, MemoryRepo
from pyedit.session import Symlink

_LINK_MODE = 0o120000


class GitPatchError(ValueError):
    pass


def apply_patch(session, text: str) -> list[str]:
    """Stage every file in `text`; returns the display paths touched.

    Fails closed: libgit2 applies the whole patch or none of it, and a
    failure rolls the session back to where it started.
    """
    if not text.strip():
        return []
    repo = MemoryRepo()
    mark = session.checkpoint()
    try:
        before = _preimage(session, repo, text)
        result = repo.apply(repo.tree(before), text)
        for path, entry in result.items():
            if entry.mode == _LINK_MODE:
                session.symlink(entry.data.decode(), path, force=True)
            else:
                session.write(path, _decoded(entry.data))
        for path in before:
            if path not in result:
                session.delete(path)
    except MemGitError as err:
        session.rollback(mark)
        raise GitPatchError(str(err)) from err
    except Exception:
        session.rollback(mark)
        raise
    return sorted(set(before) | set(result))


def _decoded(data: bytes) -> str | bytes:
    """git's rule, the one `session._slurp` uses: a NUL means bytes.

    Without this a text file replays as bytes -- the same content
    under a different type, which then diffs as a binary change.
    """
    if b"\x00" in data:
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _preimage(session, repo: MemoryRepo, text: str) -> dict:
    """Seed a tree with what the session holds for every touched path.

    libgit2 names the paths, so nothing here parses the patch either.
    A path the patch creates is absent from the tree, which is what
    `git_apply_to_tree` expects.
    """
    before = {}
    for delta in _deltas(repo, text):
        for path in (delta.old_file.path, delta.new_file.path):
            if path in before:
                continue
            entry = _session_entry(session, path)
            if entry is not None:
                before[path] = entry
    return before


def _deltas(repo: MemoryRepo, text: str):
    import pygit2

    try:
        return list(pygit2.Diff.parse_diff(text).deltas)
    except Exception as err:
        raise GitPatchError(f"libgit2 cannot read this patch: {err}") from err


_UNSET = object()


def _session_entry(session, path):
    """What the session holds for `path`, as memgit's (content, mode).

    None when nothing is there. Staged state wins over the disk: a
    script may have written text onto a path that is still a symlink
    out there.
    """
    import pygit2

    staged = session.staged().get(session.canon(path), _UNSET)
    if staged is None:
        return None
    if isinstance(staged, Symlink):
        return (str(staged), pygit2.enums.FileMode.LINK)
    if staged is not _UNSET:
        return (staged, pygit2.enums.FileMode.BLOB)
    target = _disk_link_target(path)
    if target is not None:
        return (target, pygit2.enums.FileMode.LINK)
    try:
        return (session.read(path), pygit2.enums.FileMode.BLOB)
    except FileNotFoundError:
        return None


def _disk_link_target(path) -> str | None:
    from pathlib import Path

    from pyedit.session import _disk_is_link, _disk_readlink

    p = Path(path)
    return _disk_readlink(p) if _disk_is_link(p) else None
