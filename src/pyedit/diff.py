"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), because an agent reads these diffs and
git is the format it knows. The output is a representation, not a
patch for another tool to apply: bytes and symlinks render as one-line
summaries. ``binary=True`` swaps a binary summary for a real payload,
and only the stored diffs -- dry-run ids and undo -- ask for it.

Hunk bodies come from a renderer; this module handles per-file
orchestration and headers. Two renderers exist, named for what writes
them: ``pyedit.render`` (libgit2) and ``difflib`` from the standard
library. libgit2 is the default and the only one the stored patches
use, because only it writes binary payloads and 120000 modes.
"""

from __future__ import annotations

import difflib
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


def difflib_hunks(old: str, new: str, context: int = 3) -> str:
    """Hunk bodies from the standard library, no libgit2 involved.

    Same shape as `render.DiffRenderer.hunks`: `@@` headers and bodies
    only, no file headers, empty string when nothing differs.

    Measured against `render.DiffRenderer.hunks` over changes near the
    edges, adjacent and far apart, creates, deletes and missing
    trailing newlines: the bodies and the hunk boundaries agree. One
    thing differs -- git writes the enclosing context after the `@@`
    pair (`@@ -16,5 +16,5 @@ l14`) and difflib writes none. Both are
    valid unified diffs; only git's tells you which function you are
    in. Nothing guarantees the two agree on every input.
    """
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            n=context,
        )
    )
    if not lines:
        return ""
    out = []
    for line in lines[2:]:  # difflib writes its own --- / +++ pair
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)


def unified_diffs(
    staged: dict[Path, str | bytes | None],
    context: int = 3,
    base: dict[Path, str | bytes | None] | None = None,
    replayable: bool = False,
    render=None,
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change.

    The old side comes from `base` when given (a value of None means
    the file was not there), otherwise from disk truth. Reversing a
    diff means swapping the maps: undo = unified_diffs(pre_apply,
    base=post_apply).

    `replayable` swaps the one-line note of a binary or symlink change
    for a real git section that applies back. It is off by default
    because a base85 payload is noise in a diff an agent reads; the
    stored diffs, which no one reads, turn it on.

    `render` picks what writes the hunk bodies; libgit2 by default,
    `difflib_hunks` for the standard library. It has no say over
    binary or symlink sections: only libgit2 writes those, so
    `replayable` output is always git's.
    """
    hunks_of = render or _RENDERER.hunks
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
                if replayable:
                    results.append((rel, file_patch(rel, old, new, context)))
                else:
                    results.append((rel, symlink_note(rel, old, new)))
            continue
        if isinstance(new, bytes) or isinstance(old, bytes):
            # _slurp decodes NUL-free files to str: compare encoded
            # bytes, so restaging what is on disk stays a no-op
            old_bytes = old.encode() if isinstance(old, str) else old
            new_bytes = new.encode() if isinstance(new, str) else new
            if new_bytes == old_bytes:
                continue
            if replayable:
                results.append((rel, file_patch(rel, old, new, context)))
            else:
                results.append((rel, binary_note(rel, old_bytes, new_bytes)))
            continue
        fromfile = f"a/{rel}" if old is not None else "/dev/null"
        tofile = f"b/{rel}" if new is not None else "/dev/null"
        old_text = "" if old is None else old
        new_text = "" if new is None else new
        hunks = hunks_of(old_text, new_text, context)
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


def replayable_patch(staged, context: int = 3, base=None) -> str:
    """The whole change set as one git-canonical patch.

    This is pyedit's internal representation: libgit2 writes it and
    `pyedit.gitpatch` applies it back, so nothing in between parses a
    diff. Unlike `unified_diffs` it is not for reading -- it carries
    base85 payloads and 120000 modes.
    """
    before, after = {}, {}
    for path, new in sorted(staged.items()):
        old = base.get(path) if base is not None else original(path)
        rel = display_path(path)
        before.update(_git_side(rel, old))
        after.update(_git_side(rel, new))
    if before == after:
        return ""
    from pyedit.memgit import MemoryRepo

    return MemoryRepo().patch(before, after, context)


def file_patch(rel: str, old, new, context: int = 3) -> str:
    """A git patch section for one file, written by libgit2.

    This is what a binary or symlink change needs to replay: git's own
    mode lines and base85 payloads, which pyedit has no format of its
    own for. A change of type renders as two sections, exactly as git
    writes it.
    """
    from pyedit.memgit import MemoryRepo

    repo = MemoryRepo()
    return repo.patch(_git_side(rel, old), _git_side(rel, new), context)


def _git_side(rel: str, value) -> dict:
    """One side of a change as memgit's {path: (content, mode)} map."""
    import pygit2

    if value is None:
        return {}
    if isinstance(value, Symlink):
        return {rel: (str(value), pygit2.enums.FileMode.LINK)}
    return {rel: (value, pygit2.enums.FileMode.BLOB)}


def binary_note(rel: str, old: str | bytes | None, new: str | bytes | None) -> str:
    if new is None:
        return f"Binary file {rel} deleted ({len(old)} bytes)\n"
    if old is None:
        return f"Binary file {rel} created ({len(new)} bytes)\n"
    return f"Binary file {rel} changed ({len(old)} -> {len(new)} bytes)\n"
