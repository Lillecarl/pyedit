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

from pathlib import Path

from pyedit.session import _disk_is_file, _slurp, display_path

_repo = None


def _repository():
    """An in-memory object store for blob-to-blob diffs.

    pygit2 has no OdbBackendMem, but OdbBackend is subclassable with
    the *_cb callback protocol; the store is a plain dict, so nothing
    touches disk. pygit2 is imported here rather than at module top
    because its import performs TLS setup that fails in
    certificate-less environments (nix build sandboxes; nixpkgs works
    around it the same way). A broken import should surface as a
    rendering problem, not as "import pyedit" failing.
    """
    global _repo
    if _repo is None:
        try:
            import pygit2
        except Exception as err:
            raise RuntimeError(
                f"pyedit renders diffs with libgit2 (pygit2), which failed "
                f"to import: {err}"
            ) from err

        class MemoryBackend(pygit2.OdbBackend):
            def __init__(self) -> None:
                super().__init__()
                self.store: dict[bytes, tuple[int, bytes]] = {}

            def write_cb(self, oid, data, obj_type) -> int:
                self.store[bytes(oid.raw)] = (int(obj_type), bytes(data))
                return 0

            def exists_cb(self, oid) -> bool:
                return bytes(oid.raw) in self.store

            def read_cb(self, oid):
                return self.store[bytes(oid.raw)]

            def read_header_cb(self, oid):
                obj_type, data = self.store[bytes(oid.raw)]
                return (obj_type, len(data))

            def read_prefix_cb(self, short_hex):
                for key, value in self.store.items():
                    if key.hex().startswith(short_hex):
                        return (value[0], value[1], pygit2.Oid(raw=key))
                raise KeyError(short_hex)

            def exists_prefix_cb(self, short_hex):
                for key in self.store:
                    if key.hex().startswith(short_hex):
                        return pygit2.Oid(raw=key)
                raise KeyError(short_hex)

            def refresh_cb(self) -> None:
                pass

            def __iter__(self):
                return iter([pygit2.Oid(raw=key) for key in self.store])

        backend = MemoryBackend()
        odb = pygit2.Odb()
        odb.add_backend(backend, 1)
        repo = pygit2.Repository()
        repo.set_odb(odb)
        _repo = (repo, backend)
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
    repo, backend = _repository()
    # bound the store to this one file pair
    backend.store.clear()
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
