"""Git objects that live only in memory, and `git apply` over them.

A `MemoryRepo` holds blobs and trees in libgit2's mempack object
database and applies unified diffs to them with `git_apply_to_tree`.
Nothing reaches disk: no workdir, no index file, no repository path.

Three facts shape this module. Change none of them without reading
this, because each one is a segfault or a silent wrong answer.

**The binding does not exist.** pygit2 binds `git_apply` and
`git_applies`, whose workdir and index locations both need a repository
on disk, and binds neither `git_apply_to_tree` nor `git_mempack_new`.
pygit2's own FFI is compiled (API mode) and accepts no new `cdef`, so
this opens a second cffi FFI in ABI mode over the libgit2 that pygit2
already loaded. Pointers cross between the two through the public
`_pointer` attributes.

**Never give this repository a Python ODB backend.** cffi releases the
GIL around every call it makes. pygit2 does not reacquire it in
src/odb_backend.c, so libgit2 calling a Python `OdbBackend` subclass
from any cffi call site kills the interpreter. `Index.write_tree()`
alone is enough. The mempack backend is C, so it has no callbacks and
no GIL question. `tests/test_memgit.py::test_tree_writes_into_memory`
is the pin: it crashes the whole run if the backend ever changes.

**mempack has no `read_prefix`.** `Blob.data` and `Odb.read` go through
`git_odb_read_prefix`, which for a full-length id consults the ODB
cache and then a backend `read_prefix`, so they raise KeyError for
objects that are really there. `repo[oid]` and `Blob.diff` use
`git_odb_read` and work. Read content through `MemoryRepo.read`.

To upstream this and delete the second FFI: pygit2 needs `decl/apply.h`
declaring `git_apply_to_tree`, `git_mempack_new` in `decl/odb.h`, and a
`Repository.apply_to_tree(tree, diff) -> Index` beside the existing
`apply`/`applies`. A `read_prefix` on mempack belongs upstream in
libgit2. The proof it landed: delete `_libgit2()` here, call pygit2
directly, and the test module still passes.

libgit2 applies patches far more strictly than `pyedit.udiff` does. It
checks one line position, with no outward search and no whitespace
fallback, and its parser rejects diffs without `diff --git` and mode
headers. Do not route pyedit's text patches here.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

_CDEF = """
typedef struct git_repository git_repository;
typedef struct git_tree git_tree;
typedef struct git_diff git_diff;
typedef struct git_index git_index;
typedef struct git_odb git_odb;
typedef struct git_odb_backend git_odb_backend;
typedef struct git_odb_object git_odb_object;
typedef struct { const char *message; int klass; } git_error;

int git_apply_to_tree(git_index **out, git_repository *repo,
                      git_tree *preimage, git_diff *diff, const void *opts);
int git_diff_from_buffer(git_diff **out, const char *content, size_t len);
void git_diff_free(git_diff *diff);
int git_repository_odb(git_odb **out, git_repository *repo);
void git_odb_free(git_odb *odb);
int git_odb_add_backend(git_odb *odb, git_odb_backend *backend, int priority);
int git_odb_read(git_odb_object **out, git_odb *db, const void *id);
size_t git_odb_object_size(git_odb_object *object);
const void *git_odb_object_data(git_odb_object *object);
void git_odb_object_free(git_odb_object *object);
int git_mempack_new(git_odb_backend **out);
void git_mempack_reset(git_odb_backend *backend);
const git_error *git_error_last(void);
"""


class MemGitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    """One file in an applied result."""

    path: str
    data: bytes
    mode: int


_STATE = None


def _libgit2():
    """The ABI-mode handle, opened once.

    Deferred like pygit2's own import in `pyedit.render`: importing
    pygit2 performs TLS setup that fails where no certificates are
    installed, so a broken environment surfaces here and not at
    module import.
    """
    global _STATE
    if _STATE is not None:
        return _STATE
    try:
        import cffi
        import pygit2
    except Exception as err:
        raise MemGitError(
            f"pyedit applies patches in memory with libgit2 (pygit2 and "
            f"cffi), which failed to import: {err}"
        ) from err
    ffi = cffi.FFI()
    ffi.cdef(_CDEF)
    name = _library_name(pygit2)
    try:
        lib = ffi.dlopen(name)
    except Exception as err:
        raise MemGitError(
            f"pyedit needs the libgit2 that pygit2 {pygit2.__version__} "
            f"loaded, and dlopen({name!r}) failed: {err}"
        ) from err
    _STATE = (ffi, lib, pygit2)
    return _STATE


def _library_name(pygit2) -> str:
    """The libgit2 already mapped into this process.

    dlopen of a library that is loaded returns that same instance, so
    the absolute path out of the process map is exact. It must still
    match pygit2's own version: two libgit2 store paths can be mapped
    at once, and the wrong one means casting pointers across ABIs.
    The soname is the fallback for platforms with no /proc.
    """
    major, minor = pygit2.LIBGIT2_VER[:2]
    soname = f"libgit2.so.{major}.{minor}"
    try:
        with open("/proc/self/maps") as maps:
            for line in maps:
                match = re.search(rf"\S+/{re.escape(soname)}\S*$", line)
                if match:
                    return match.group(0)
    except OSError:
        pass
    return soname


class MemoryRepo:
    """A git object store with no disk behind it."""

    def __init__(self) -> None:
        ffi, lib, pygit2 = _libgit2()
        self._ffi = ffi
        self._lib = lib
        self._pygit2 = pygit2
        self._repo = pygit2.Repository()
        self._repo.set_odb(pygit2.Odb())
        out = ffi.new("git_odb **")
        self._check(lib.git_repository_odb(out, self._as("git_repository *", self._repo)))
        self._odb = ffi.gc(out[0], lib.git_odb_free)
        backend = ffi.new("git_odb_backend **")
        self._check(lib.git_mempack_new(backend))
        # the odb owns the backend from here and frees it with itself
        self._check(lib.git_odb_add_backend(self._odb, backend[0], 999))
        self._backend = backend[0]

    def reset(self) -> None:
        """Drop every object written so far."""
        self._lib.git_mempack_reset(self._backend)

    def write(self, data: bytes | str):
        """Store a blob; returns its Oid."""
        if isinstance(data, str):
            data = data.encode()
        return self._repo.create_blob(data)

    def blob(self, data: bytes | str):
        """Store a blob and return the pygit2 object.

        Its `.data` raises KeyError - see the read_prefix note above.
        `diff()` on it works, and `read` gives the content.
        """
        return self._repo[self.write(data)]

    def read(self, oid) -> bytes:
        out = self._ffi.new("git_odb_object **")
        self._check(
            self._lib.git_odb_read(out, self._odb, self._ffi.from_buffer(oid.raw)),
            f"read {oid}",
        )
        obj = self._ffi.gc(out[0], self._lib.git_odb_object_free)
        return bytes(
            self._ffi.buffer(
                self._lib.git_odb_object_data(obj), self._lib.git_odb_object_size(obj)
            )
        )

    def tree(self, files):
        """Build a tree from {path: content} or {path: (content, mode)}."""
        index = self._pygit2.Index()
        for path, value in files.items():
            content, mode = value if isinstance(value, tuple) else (value, None)
            if mode is None:
                mode = self._pygit2.enums.FileMode.BLOB
            index.add(self._pygit2.IndexEntry(path, self.write(content), mode))
        return self._repo[index.write_tree(self._repo)]

    def merge(self, ancestor, ours, theirs, find_renames: bool = True):
        """Three-way merge of three trees; returns the index.

        A conflicted path carries every stage, so iterating the result
        yields it once per stage. Read `conflicts` first and skip those
        paths, or the last stage silently wins.

        `find_renames` pairs a delete with an add by content
        similarity, not by history, so an edit to the deleted path
        moves to the added one. libgit2 turns it on by default and so
        does this. It is the one place the merge answers by
        similarity rather than by reading the ancestor, hence the
        switch.
        """
        import pygit2

        flags = pygit2.enums.MergeFlag.FIND_RENAMES if find_renames else 0
        return self._repo.merge_trees(ancestor, ours, theirs, flags=flags)

    def patch(self, before, after, context: int = 3) -> str:
        """The git patch that turns one file map into the other.

        libgit2 writes it, so the mode lines a symlink needs (120000)
        and the base85 payload a binary change needs are both there,
        and `apply` takes the result back.
        """
        diff = self.tree(before).diff_to_tree(
            self.tree(after),
            flags=self._pygit2.enums.DiffOption.SHOW_BINARY,
            context_lines=context,
        )
        return diff.patch or ""

    def apply(self, tree, patch: str | bytes) -> dict[str, Entry]:
        """Apply a git patch to a tree; returns the whole postimage.

        Every path the patch touches must already be in the tree, and
        one hunk that does not anchor fails the call: libgit2 applies
        all of the patch or none of it.
        """
        data = patch.encode() if isinstance(patch, str) else patch
        out = self._ffi.new("git_diff **")
        self._check(self._lib.git_diff_from_buffer(out, data, len(data)), "parse")
        diff = self._ffi.gc(out[0], self._lib.git_diff_free)
        index_out = self._ffi.new("git_index **")
        self._check(
            self._lib.git_apply_to_tree(
                index_out,
                self._as("git_repository *", self._repo),
                self._as("git_tree *", tree),
                diff,
                self._ffi.NULL,
            ),
            "apply",
        )
        holder = self._pygit2.ffi.new("git_index **")
        self._pygit2.ffi.buffer(holder)[:] = self._ffi.buffer(index_out)[:]
        # Index.from_c owns the git_index from here
        result = self._pygit2.Index.from_c(self._repo, holder)
        return {
            entry.path: Entry(entry.path, self.read(entry.id), int(entry.mode))
            for entry in result
        }

    def _as(self, kind, obj):
        return self._ffi.cast(kind, int.from_bytes(obj._pointer, sys.byteorder))

    def _check(self, code: int, doing: str = "") -> None:
        if not code:
            return
        err = self._lib.git_error_last()
        message = (
            self._ffi.string(err.message).decode() if err != self._ffi.NULL else "no detail"
        )
        where = f"{doing}: " if doing else ""
        raise MemGitError(f"libgit2 {where}{message} (code {code})")


def apply_to_files(files, patch: str | bytes) -> dict[str, Entry]:
    """Apply a git patch to {path: content}; returns the postimage."""
    repo = MemoryRepo()
    return repo.apply(repo.tree(files), patch)
