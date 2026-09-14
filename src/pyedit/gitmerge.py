"""Merge a VFS scope into its parent with git's three-way merge.

The job is already three-way, so libgit2 fits it exactly:

    ancestor  disk truth for every touched path
    ours      what the parent has staged
    theirs    what the scope staged

``merge_trees`` takes the ancestor as an argument, so no commits are
needed: a scope is a tree plus a remembered ancestor.

git decides, and pyedit does not argue with it. git merges two changed
regions only when an unchanged line separates them; two edits to
neighbouring lines are a conflict, and they belong in one scope. Being
cleverer than git is not a goal here -- a better engine would be a
better library, not a second algorithm beside this one.

Rename detection follows git and is on. It is the one place the merge
answers by content similarity rather than by reading the ancestor, so
`--no-rename-detection` turns it off and the rename collides instead.

Two traps, each one a silently wrong answer:

**A conflicted path is yielded once per stage.** Iterating the merged
index gives every stage of a conflict, not just stage 0. Take them in
order and the last stage wins, which resolves the conflict to "theirs"
with no error at all.

**Every side must carry the ancestor's entry for a path it does not
touch.** A tree built from one side's overlay alone reads as "this side
deleted everything it did not mention", and the merge deletes the
repository.
"""

from __future__ import annotations

from pathlib import Path

from pyedit.memgit import MemoryRepo
from pyedit.merge import Collision
from pyedit.session import (
    Symlink,
    _disk_is_file,
    _disk_is_link,
    _disk_readlink,
    _slurp,
)

_LINK_MODE = 0o120000


def merge(parent, child) -> None:
    """Stage the scope's changes on the parent, resolved by git.

    Fails closed: one conflict raises and the parent keeps the state it
    had, so a scope merges whole or not at all.
    """
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
        find_renames=parent._find_renames,
    )

    # every conflict is reported before anything is staged
    if merged.conflicts is not None:
        raise Collision(
            "; ".join(
                sorted(_conflict(repo, back, triple) for triple in merged.conflicts)
            )
        )

    seen = set()
    for entry in merged:
        path = back[entry.path]
        seen.add(path)
        data = repo.read(entry.id)
        if entry.mode == _LINK_MODE:
            parent._stage(path, Symlink(data.decode()))
        else:
            parent._stage(path, _decoded(data))
    for path in paths:
        if path not in seen:
            parent._stage(path, None)


def _conflict(repo: MemoryRepo, back: dict, triple) -> str:
    ancestor, ours, theirs = triple
    path = back[(ancestor or ours or theirs).path]
    if ours is None:
        return f"{path}: deleted by an earlier edit, changed here"
    if theirs is None:
        return f"{path}: changed by an earlier edit, deleted here"
    if ancestor is None:
        return f"{path}: created by two edits with different content"
    if any(b"\x00" in repo.read(entry.id) for entry in (ours, theirs)):
        return f"{path}: binary file changed by two edits"
    return (
        f"{path}: two edits changed lines with nothing unchanged between "
        "them; git will not merge that, so make both edits in one scope"
    )


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
