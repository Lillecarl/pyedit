"""libgit2-backed unified-diff hunk renderer.

`DiffRenderer.hunks()` returns the `@@` hunk body for two texts - git
conventions, including the ``\\ No newline at end of file`` markers for
unterminated content. Only the hunk body: callers assemble their own
file headers.

The pygit2 import is deferred to the first render because importing
pygit2 performs TLS setup that fails in certificate-less environments
(nix build sandboxes; nixpkgs works around it the same way). A broken
import surfaces as a rendering error, not as a failed module import.

Diffing reads straight from the blobs' data pointers (git_diff_blobs),
so the object store they live in is the only state libgit2 needs -
here a dict-backed OdbBackend in a backend-less Repository, entirely
in memory. pygit2 has no OdbBackendMem; OdbBackend is subclassable
with the *_cb callback protocol (upstream test_odb_backend.py is the
contract). The store is cleared per hunk call, so nothing accumulates.
"""

from __future__ import annotations


class DiffRenderer:
    def __init__(self) -> None:
        self._repo = None
        self._backend = None

    def hunks(self, a_text: str, b_text: str, context: int = 3) -> str:
        repo, backend = self._repository()
        backend.store.clear()
        old = repo[repo.create_blob(a_text.encode("utf-8"))]
        new = repo[repo.create_blob(b_text.encode("utf-8"))]
        body = old.diff(new, context_lines=context).text or ""
        lines = body.splitlines(keepends=True)
        index = 0
        while index < len(lines) and not lines[index].startswith("@@"):
            index += 1
        return "".join(lines[index:])

    def _repository(self):
        if self._repo is not None:
            return self._repo, self._backend
        try:
            import pygit2
        except Exception as err:
            raise RuntimeError(
                f"pyedit renders diffs with libgit2 (pygit2), which failed "
                f"to import: {err}"
            ) from err
        self._backend = _memory_backend(pygit2)
        odb = pygit2.Odb()
        odb.add_backend(self._backend, 1)
        repo = pygit2.Repository()
        repo.set_odb(odb)
        self._repo = repo
        return self._repo, self._backend


def _memory_backend(pygit2):
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

    return MemoryBackend()
