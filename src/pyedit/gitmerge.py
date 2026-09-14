"""Merge a VFS scope into its parent with git's three-way merge.

PROTOTYPE. Under evaluation against `merge.py`, which does the same
job by re-anchoring hunks on context.

The shape of the job is already three-way, so libgit2 fits it exactly:

    ancestor  disk truth for every touched path
    ours      what the parent has staged
    theirs    what the scope staged

`Repository.merge_trees` takes the ancestor as an argument, so no
commits are needed -- a scope is a tree plus a remembered ancestor.

Every side must carry the ancestor's entry for a path it does not
touch. A tree built from one side's overlay alone reads as "this side
deleted everything it did not mention", and the merge deletes the
repository.
"""

from __future__ import annotations

from pathlib import Path

from pyedit.memgit import MemoryRepo
from pyedit.session import Symlink, _disk_is_file, _disk_is_link, _disk_readlink, _slurp


class GitMergeError(ValueError):
    pass


def merge(parent, child) -> None:
    """Stage the scope's changes on the parent, resolved by git."""
    child.prune_unchanged()
    theirs = child.staged()
    ours = parent.staged()
    paths = sorted(set(theirs) | set(ours))
    if not paths:
        return

    keys = {path: _key(parent, path) for path in paths}
    back = {key: path for path, key in keys.items()}

    repo = MemoryRepo()
    base = {}
    for path in paths:
        entry = _disk_entry(path)
        if entry is not None:
            base[keys[path]] = entry

    merged = repo.merge(
        repo.tree(base),
        repo.tree(_side(base, keys, ours)),
        repo.tree(_side(base, keys, theirs)),
    )
    stuck = set()
    if merged.conflicts is not None:
        stuck = {(a or b or c).path for a, b, c in merged.conflicts}
    conflicted = {back[key] for key in stuck}

    # The index carries full paths; a Tree only yields its top level.
    # Iterating a conflicted index yields every stage of a conflicted
    # path, not just stage 0, so those must be skipped by hand -- take
    # them and the last stage silently wins.
    seen = set()
    for entry in merged:
        if entry.path in stuck:
            continue
        path = back[entry.path]
        seen.add(path)
        data = repo.read(entry.id)
        if entry.mode == 0o120000:
            parent._stage(path, Symlink(data.decode()))
        else:
            parent._stage(path, _decoded(data))
    for path in paths:
        if path not in seen and path not in conflicted:
            parent._stage(path, None)

    # git conflicts on changed regions with no unchanged line between
    # them -- two scopes editing neighbouring lines. That is the case
    # pyedit exists for, so its context re-anchoring gets the path.
    if conflicted:
        from pyedit.merge import _merge_one

        for path in sorted(conflicted):
            parent._stage(path, _merge_one(parent, path, theirs[path]))


def _side(base: dict, keys: dict, overlay: dict) -> dict:
    """The ancestor, with this side's opinions layered on top."""
    side = dict(base)
    for path, value in overlay.items():
        key = keys[path]
        if value is None:
            side.pop(key, None)
        else:
            side[key] = _entry(value)
    return side


def _entry(value):
    import pygit2

    if isinstance(value, Symlink):
        return (str(value), pygit2.enums.FileMode.LINK)
    if isinstance(value, str):
        return (value.encode(), pygit2.enums.FileMode.BLOB)
    return (value, pygit2.enums.FileMode.BLOB)


def _disk_entry(path: Path):
    if _disk_is_link(path):
        return _entry(Symlink(_disk_readlink(path)))
    if not _disk_is_file(path):
        return None
    return _entry(_slurp(path))


def _key(session, path: Path) -> str:
    """A tree path for an overlay path.

    Tree paths are relative and may not escape the root, so a path
    outside it gets a flattened name. Only the merge sees these.
    """
    try:
        return path.relative_to(session.root).as_posix()
    except ValueError:
        return "__outside__/" + str(path).lstrip("/").replace("/", "%2F")


def _decoded(data: bytes):
    """git's rule: a NUL makes it binary, everything else is text."""
    if b"\x00" in data:
        return data
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data
