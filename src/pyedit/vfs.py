"""Stdlib monkeypatch layer: route file writes into the session overlay.

During a script run, writes through ``open()``, ``pathlib.Path``, ``os``
and ``shutil`` are captured in memory and end up in the diff; disk is
touched only when the caller applies. Reads consult the overlay first,
so read-your-writes holds across every API. ``install`` returns a
callable that puts every patched attribute back.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
from pathlib import Path
from typing import Callable

from pyedit.session import EditSession, _MISSING, glob_re


class _StagedTextIO(io.StringIO):
    def __init__(self, session: EditSession, path: Path, seed: str) -> None:
        super().__init__(seed)
        self._session = session
        self._path = path
        self.name = str(path)

    def close(self) -> None:
        if not self.closed:
            self._session.write(self._path, self.getvalue())
        super().close()


class _StagedBytesIO(io.BytesIO):
    def __init__(self, session: EditSession, path: Path, seed: bytes) -> None:
        super().__init__(seed)
        self._session = session
        self._path = path
        self.name = str(path)

    def close(self) -> None:
        if not self.closed:
            self._session.write(self._path, self.getvalue())
        super().close()


class _ReadOnlyIO:
    def __init__(self, buf: io.IOBase, name: str) -> None:
        self._buf = buf
        self.name = name

    def read(self, *args):
        return self._buf.read(*args)

    def readline(self, *args):
        return self._buf.readline(*args)

    def readlines(self, *args):
        return self._buf.readlines(*args)

    def seek(self, *args):
        return self._buf.seek(*args)

    def tell(self):
        return self._buf.tell()

    def write(self, *args):
        raise io.UnsupportedOperation("File not open for writing")

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    @property
    def closed(self):
        return self._buf.closed

    def close(self):
        self._buf.close()

    def __iter__(self):
        return self._buf.__iter__()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def install(session: EditSession) -> Callable[[], None]:
    real_open = builtins.open
    path_open = Path.open
    path_write_text = Path.write_text
    path_write_bytes = Path.write_bytes
    path_unlink = Path.unlink
    path_exists = Path.exists
    path_is_file = Path.is_file
    path_is_dir = Path.is_dir
    path_read_text = Path.read_text
    path_read_bytes = Path.read_bytes
    path_iterdir = Path.iterdir
    path_glob = Path.glob
    path_mkdir = Path.mkdir
    path_rmdir = Path.rmdir
    path_rename = Path.rename
    path_replace = Path.replace
    os_remove = os.remove
    os_rename = os.rename
    os_replace = os.replace
    os_mkdir = os.mkdir
    os_makedirs = os.makedirs
    os_listdir = os.listdir
    os_path_exists = os.path.exists
    os_path_isfile = os.path.isfile
    os_path_isdir = os.path.isdir
    sh_copyfile = shutil.copyfile
    sh_copy = shutil.copy
    sh_copy2 = shutil.copy2
    sh_copytree = shutil.copytree
    sh_move = shutil.move
    sh_rmtree = shutil.rmtree

    def resolve(path) -> Path:
        return Path(path).resolve()

    def staged(path):
        return session.staged_content(resolve(path))

    def coerce(content, binary: bool):
        if isinstance(content, bytes):
            return content if binary else content.decode("utf-8")
        return content.encode("utf-8") if binary else content

    def seed(path: Path, binary: bool, truncate: bool):
        content = staged(path)
        if content is not _MISSING:
            if content is None:
                return b"" if binary else ""
            return coerce(content, binary)
        if truncate:
            return b"" if binary else ""
        if binary:
            return path.read_bytes() if path.is_file() else b""
        return path.read_text()

    def _open(file, mode="r", *args, **kwargs):
        if isinstance(file, int):
            return real_open(file, mode, *args, **kwargs)
        p = resolve(file)
        flags = set(mode)
        binary = "b" in flags
        writing = bool(flags & {"w", "a", "x", "+"})
        if not writing:
            content = staged(p)
            if content is None:
                raise FileNotFoundError(str(p))
            if content is not _MISSING:
                buf = (
                    io.BytesIO(coerce(content, True))
                    if binary
                    else io.StringIO(coerce(content, False))
                )
                return _ReadOnlyIO(buf, str(p))
            return real_open(file, mode, *args, **kwargs)
        if "x" in flags and session.exists(p):
            raise FileExistsError(str(p))
        seed_data = seed(p, binary, truncate="w" in flags or "x" in flags)
        if binary:
            stream = _StagedBytesIO(session, p, seed_data)
        else:
            stream = _StagedTextIO(session, p, seed_data)
        if "a" in flags:
            stream.seek(0, io.SEEK_END)
        return stream

    def _move(source: Path, dest: Path) -> None:
        content = staged(source)
        if content is None or (content is _MISSING and not source.is_file()):
            raise FileNotFoundError(str(source))
        if dest.is_dir():
            raise IsADirectoryError(str(dest))
        if source == dest:
            return
        session.write(dest, session.current(source))
        session.delete(source)

    def _copyfile(source: Path, dest: Path) -> Path:
        if source == dest:
            raise shutil.SameFileError(f"{source!r} and {dest!r} are the same file")
        if dest.is_dir():
            raise IsADirectoryError(str(dest))
        session.write(dest, session.current(source))
        return dest

    def _stage_tree(source: Path, dest: Path) -> None:
        for entry in session.entries(source):
            if entry.is_dir():
                _stage_tree(entry, dest / entry.name)
            else:
                session.write(dest / entry.name, session.current(entry))

    def _open_path(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return _open(self, mode, buffering, encoding, errors, newline)

    def _write_text(self, data, *args, **kwargs):
        if not isinstance(data, str):
            raise TypeError(f"data must be str, not {type(data).__name__}")
        session.write(resolve(self), data)
        return len(data)

    def _write_bytes(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"a bytes-like object is required, not {type(data).__name__}")
        session.write(resolve(self), bytes(data))
        return len(data)

    def _unlink(self, missing_ok=False):
        try:
            session.delete(resolve(self))
        except FileNotFoundError:
            if not missing_ok:
                raise

    def _exists(self, *args, **kwargs):
        content = staged(self)
        if content is not _MISSING:
            return content is not None
        return path_exists(self, *args, **kwargs)

    def _is_file(self, *args, **kwargs):
        content = staged(self)
        if content is not _MISSING:
            return content is not None
        return path_is_file(self, *args, **kwargs)

    def _is_dir(self, *args, **kwargs):
        return path_is_dir(self, *args, **kwargs)

    def _read_text(self, encoding=None, errors=None, newline=None):
        content = staged(self)
        if content is None:
            raise FileNotFoundError(str(self))
        if content is not _MISSING:
            return coerce(content, False)
        return path_read_text(self, encoding=encoding, errors=errors, newline=newline)

    def _read_bytes(self):
        content = staged(self)
        if content is None:
            raise FileNotFoundError(str(self))
        if content is not _MISSING:
            return coerce(content, True)
        return path_read_bytes(self)

    def _iterdir(self):
        return iter(session.entries(self))

    def _glob(self, pattern, *, case_sensitive=None, recurse_symlinks=False):
        base = resolve(self)
        rx = glob_re(pattern)
        out: dict[Path, Path] = {}
        for match in path_glob(
            self, pattern, case_sensitive=case_sensitive, recurse_symlinks=recurse_symlinks
        ):
            resolved = resolve(match)
            if staged(resolved) is None:
                continue
            out[resolved] = resolved
        for path, content in session.staged().items():
            if content is None:
                continue
            try:
                rel = path.relative_to(base)
            except ValueError:
                continue
            if rx.match(rel.as_posix()):
                out.setdefault(path, path)
        return iter([out[key] for key in sorted(out)])

    def _mkdir(self, mode=0o777, parents=False, exist_ok=False):
        p = resolve(self)
        if p.is_dir():
            if exist_ok:
                return
            raise FileExistsError(str(p))
        if not parents and not p.parent.is_dir():
            raise FileNotFoundError(str(p))
        # dirs are untracked: created on apply when a staged file needs one

    def _rmdir(self):
        p = resolve(self)
        if not p.exists():
            raise FileNotFoundError(str(p))
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        if session.entries(p):
            raise OSError(f"Directory not empty: {p}")
        # dirs are untracked: an empty dir survives apply

    def _path_rename(self, target):
        _move(resolve(self), resolve(target))
        return Path(target)

    def _os_remove(path, /):
        session.delete(resolve(path))

    def _os_mkdir(path, mode=0o777):
        return _mkdir(Path(path))

    def _os_makedirs(name, mode=0o777, exist_ok=False):
        p = resolve(name)
        if p.is_dir():
            if exist_ok:
                return
            raise FileExistsError(str(p))
        # dirs are untracked: created on apply when a staged file needs one

    def _os_listdir(path="."):
        return [entry.name for entry in session.entries(resolve(path))]

    def _os_walk(top, topdown=True, onerror=None, followlinks=False):
        top_path = resolve(top)
        try:
            listing = session.entries(top_path)
        except OSError as err:
            if onerror is not None:
                onerror(err)
            return
        dirs = [e for e in listing if e.is_dir() and (followlinks or not e.is_symlink())]
        files = [e for e in listing if not e.is_dir()]
        if topdown:
            yield str(top_path), [d.name for d in dirs], [f.name for f in files]
        for d in dirs:
            yield from _os_walk(d, topdown, onerror, followlinks)
        if not topdown:
            yield str(top_path), [d.name for d in dirs], [f.name for f in files]

    def _os_path_exists(path):
        return session.exists(path)

    def _os_path_isfile(path):
        return session.is_file(path)

    def _os_path_isdir(path):
        return session.is_dir(path)

    def _os_rename(src, dst, /, *args, **kwargs):
        _move(resolve(src), resolve(dst))

    def _shutil_copyfile(src, dst, *, follow_symlinks=True):
        return str(_copyfile(resolve(src), resolve(dst)))

    def _shutil_copy(src, dst, **kwargs):
        dest = resolve(dst)
        if dest.is_dir():
            dest = dest / resolve(src).name
        _copyfile(resolve(src), dest)
        return str(dest)

    def _shutil_copy2(src, dst, **kwargs):
        return _shutil_copy(src, dst)

    def _shutil_copytree(src, dst, *, dirs_exist_ok=False, **kwargs):
        if kwargs:
            raise NotImplementedError(
                "copytree: only (src, dst, dirs_exist_ok) are supported"
            )
        source = resolve(src)
        dest = resolve(dst)
        if not source.is_dir():
            raise FileNotFoundError(str(source))
        if dest.is_dir() and not dirs_exist_ok:
            raise FileExistsError(str(dest))
        _stage_tree(source, dest)
        return dest

    def _shutil_move(src, dst):
        source = resolve(src)
        dest = resolve(dst)
        if dest.is_dir():
            dest = dest / source.name
        session.write(dest, session.current(source))
        if source != dest:
            session.delete(source)
        return str(dest)

    def _shutil_rmtree(path, ignore_errors=False, **kwargs):
        try:
            p = resolve(path)
            if p.is_dir():
                for child in p.rglob("*"):
                    if child.is_file():
                        session.delete(child)
            elif p.is_file():
                raise NotADirectoryError(str(p))
            elif not any(k != p and k.is_relative_to(p) for k in session.staged()):
                raise FileNotFoundError(str(p))
            for staged_path in session.staged():
                if staged_path != p and staged_path.is_relative_to(p):
                    session.delete(staged_path)
        except OSError:
            if not ignore_errors:
                raise

    patches = [
        (builtins, "open", _open),
        (Path, "open", _open_path),
        (Path, "write_text", _write_text),
        (Path, "write_bytes", _write_bytes),
        (Path, "unlink", _unlink),
        (Path, "exists", _exists),
        (Path, "is_file", _is_file),
        (Path, "is_dir", _is_dir),
        (Path, "read_text", _read_text),
        (Path, "read_bytes", _read_bytes),
        (Path, "iterdir", _iterdir),
        (Path, "glob", _glob),
        (Path, "mkdir", _mkdir),
        (Path, "rmdir", _rmdir),
        (Path, "rename", _path_rename),
        (Path, "replace", _path_rename),
        (os, "remove", _os_remove),
        (os, "unlink", _os_remove),
        (os, "rename", _os_rename),
        (os, "replace", _os_rename),
        (os, "mkdir", _os_mkdir),
        (os, "makedirs", _os_makedirs),
        (os, "listdir", _os_listdir),
        (os, "walk", _os_walk),
        (os.path, "exists", _os_path_exists),
        (os.path, "isfile", _os_path_isfile),
        (os.path, "isdir", _os_path_isdir),
        (shutil, "copyfile", _shutil_copyfile),
        (shutil, "copy", _shutil_copy),
        (shutil, "copy2", _shutil_copy2),
        (shutil, "copytree", _shutil_copytree),
        (shutil, "move", _shutil_move),
        (shutil, "rmtree", _shutil_rmtree),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
    for owner, name, replacement in patches:
        setattr(owner, name, replacement)

    def restore() -> None:
        for owner, name, original in originals:
            setattr(owner, name, original)

    return restore
