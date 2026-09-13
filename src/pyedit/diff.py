"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), because an agent reads these diffs and
git is the format it knows. The output is a representation, not a
patch for another tool to apply: bytes and symlinks render as one-line
summaries. ``binary=True`` swaps a binary summary for a real payload,
and only the stored diffs -- dry-run ids and undo -- ask for it.

Hunk bodies come from ``pyedit.render`` (libgit2); this module handles
per-file orchestration and headers.
"""

from __future__ import annotations

from pathlib import Path

from pyedit.render import DiffRenderer
from pyedit.session import (
    Symlink,
    _disk_is_file,
    _disk_is_link,
    _disk_readlink,
    _slurp,
    display_path,
)

# one process-wide renderer; it imports pygit2 lazily, so module import
# stays cheap and cert-less environments fail at render time only
_RENDERER = DiffRenderer()


def _encoded(value):
    return value.encode() if isinstance(value, str) else value


def original(path: Path) -> str | bytes | None:
    if _disk_is_link(path):
        return Symlink(_disk_readlink(path))
    if not _disk_is_file(path):
        return None
    return _slurp(path)


def unified_diffs(
    staged: dict[Path, str | bytes | None],
    context: int = 3,
    base: dict[Path, str | bytes | None] | None = None,
    binary: bool = False,
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change.

    The old side comes from `base` when given (a value of None means
    the file was not there), otherwise from disk truth. Reversing a
    diff means swapping the maps: undo = unified_diffs(pre_apply,
    base=post_apply).

    `binary` swaps the one-line summary of a binary change for a real
    `GIT binary patch` section, which re-applies. It is off by default
    because the payload is base85 noise in a diff an agent reads; the
    undo diff, which is stored and never printed, turns it on.
    """
    entries = []
    for path, new in sorted(staged.items()):
        old = base.get(path) if base is not None else original(path)
        if new is None and old is None:
            continue
        entries.append((path, old, new))
    # a staged deletion and a staged creation with the same
    # content are one rename: render it with git's extended
    # headers instead of a verbose delete+create pair
    deletions = [e for e in entries if e[1] is not None and e[2] is None]
    creations = [
        e
        for e in entries
        if e[1] is None and e[2] is not None and not isinstance(e[2], Symlink)
    ]
    partners: dict[Path, Path] = {}
    consumed: set[Path] = set()
    for dpath, dold, _ in deletions:
        if isinstance(dold, Symlink):
            continue
        for cpath, _cold, cnew in creations:
            if cpath in consumed:
                continue
            if _encoded(dold) == _encoded(cnew):
                partners[dpath] = cpath
                consumed.add(cpath)
                break
    results: list[tuple[str, str]] = []
    for path, old, new in entries:
        if path in partners:
            rel = display_path(path)
            to = display_path(partners[path])
            results.append(
                (
                    rel,
                    f"diff --git a/{rel} b/{to}\n"
                    f"similarity index 100%\n"
                    f"rename from {rel}\n"
                    f"rename to {to}\n"
                    f"--- a/{rel}\n"
                    f"+++ b/{to}\n",
                )
            )
            continue
        if path in consumed:
            continue
        rel = display_path(path)
        if isinstance(new, Symlink) or isinstance(old, Symlink):
            if new != old:
                results.append((rel, symlink_note(rel, old, new)))
            continue
        if isinstance(new, bytes) or isinstance(old, bytes):
            # _slurp decodes NUL-free files to str: compare encoded
            # bytes, so restaging what is on disk stays a no-op
            old_bytes = old.encode() if isinstance(old, str) else old
            new_bytes = new.encode() if isinstance(new, str) else new
            if new_bytes == old_bytes:
                continue
            if binary:
                results.append((rel, binary_patch(rel, old_bytes, new_bytes)))
            else:
                results.append((rel, binary_note(rel, old_bytes, new_bytes)))
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


NOTE_PREFIXES = ("Symlink ", "Binary file ")


def symlink_note(rel: str, old, new) -> str:
    if isinstance(new, Symlink) and old is None:
        return f"Symlink {rel} -> {new} created\n"
    if isinstance(old, Symlink) and new is None:
        return f"Symlink {rel} -> {old} deleted\n"
    if isinstance(new, Symlink) and isinstance(old, Symlink):
        return f"Symlink {rel} retargeted ({old} -> {new})\n"
    if isinstance(new, Symlink):
        return f"Symlink {rel} -> {new} (replaces a regular file)\n"
    return f"Symlink {rel} -> {old} replaced by a regular file\n"


_ZERO_OID = "0" * 40


def binary_patch(rel: str, old: bytes | None, new: bytes | None) -> str:
    """A `git diff --binary` section for one file.

    libgit2 writes the payload and pyedit adds the headers it needs to
    parse back: the paths, the mode line for a create or a delete, and
    the blob ids. pyedit does not track file modes, so every regular
    file is 100644.
    """
    from pyedit.memgit import MemoryRepo

    import pygit2

    repo = MemoryRepo()
    old_bytes = b"" if old is None else old
    new_bytes = b"" if new is None else new
    body = (
        repo.blob(old_bytes)
        .diff(repo.blob(new_bytes), flags=pygit2.enums.DiffOption.SHOW_BINARY)
        .text
    )
    old_oid = _ZERO_OID if old is None else str(repo.write(old_bytes))
    new_oid = _ZERO_OID if new is None else str(repo.write(new_bytes))
    header = f"diff --git a/{rel} b/{rel}\n"
    if old is None:
        header += "new file mode 100644\n"
    elif new is None:
        header += "deleted file mode 100644\n"
    header += f"index {old_oid}..{new_oid} 100644\n"
    return header + "GIT binary patch" + body.split("GIT binary patch", 1)[1]


def binary_note(rel: str, old: str | bytes | None, new: str | bytes | None) -> str:
    if new is None:
        return f"Binary file {rel} deleted ({len(old)} bytes)\n"
    if old is None:
        return f"Binary file {rel} created ({len(new)} bytes)\n"
    return f"Binary file {rel} changed ({len(old)} -> {len(new)} bytes)\n"
