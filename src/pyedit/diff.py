"""Unified diff rendering for staged changes.

Headers follow git conventions (``a/`` and ``b/`` prefixes, ``/dev/null``
for added and deleted files), so text output can be piped to ``git
apply`` or ``patch -p1``. Changes involving bytes render as one-line
summaries instead of hunks.

Hunk bodies come from libgit2 (pygit2) - git's own diff engine - which
gets the git conventions right, including the
``\\ No newline at end of file`` markers for unterminated content.
Only the file headers are ours.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyedit.session import _disk_is_file, _slurp, display_path

_repo = None


def _repository():
    # throwaway object store for blob-to-blob diffs; nothing is read
    # from it except the blobs we just wrote. pygit2 is imported here
    # rather than at module top because its import performs TLS setup
    # that fails in certificate-less environments (nix build sandboxes;
    # nixpkgs works around it the same way). A broken import should
    # surface as a rendering problem, not as "import pyedit" failing.
    global _repo
    if _repo is None:
        try:
            import pygit2
        except Exception as err:
            raise RuntimeError(
                f"pyedit renders diffs with libgit2 (pygit2), which failed "
                f"to import: {err}"
            ) from err
        _repo = pygit2.init_repository(
            tempfile.mkdtemp(prefix="pyedit-diff-"), bare=True
        )
    return _repo


def original(path: Path) -> str | bytes | None:
    if not _disk_is_file(path):
        return None
    return _slurp(path)


def unified_diffs(
    staged: dict[Path, str | bytes | None], context: int = 3
) -> list[tuple[str, str]]:
    """Return (display path, diff text) for every staged change."""
    results: list[tuple[str, str]] = []
    for path, new in sorted(staged.items()):
        old = original(path)
        if new is None and old is None:
            continue
        rel = display_path(path)
        if isinstance(new, bytes) or isinstance(old, bytes):
            results.append((rel, binary_note(rel, old, new)))
            continue
        fromfile = f"a/{rel}" if old is not None else "/dev/null"
        tofile = f"b/{rel}" if new is not None else "/dev/null"
        diff = _render(
            "" if old is None else old,
            "" if new is None else new,
            fromfile,
            tofile,
            context,
        )
        if diff:
            results.append((rel, diff))
    return results


def _render(a_text: str, b_text: str, fromfile: str, tofile: str, context: int) -> str:
    repo = _repository()
    old = repo[repo.create_blob(a_text.encode("utf-8"))]
    new = repo[repo.create_blob(b_text.encode("utf-8"))]
    body = old.diff(new, context_lines=context).text or ""
    lines = body.splitlines(keepends=True)
    index = 0
    while index < len(lines) and not lines[index].startswith("@@"):
        index += 1
    hunks = "".join(lines[index:])
    if not hunks:
        return ""
    return f"--- {fromfile}\n+++ {tofile}\n" + hunks


def binary_note(rel: str, old: str | bytes | None, new: str | bytes | None) -> str:
    if new is None:
        return f"Binary file {rel} deleted ({len(old)} bytes)\n"
    if old is None:
        return f"Binary file {rel} created ({len(new)} bytes)\n"
    return f"Binary file {rel} changed ({len(old)} -> {len(new)} bytes)\n"
