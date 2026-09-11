"""In-memory write overlay for edit scripts.

The session is injected into edit scripts as the global name ``pyedit``.
State is a plain dict keyed by resolved path: str or bytes content,
None for deletions. Every write lands in this dict; disk is touched
only when the caller applies the staged changes. Reads see staged
content, so scripts get read-your-writes semantics.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_MISSING = object()

_GLOB_CACHE: dict[str, re.Pattern] = {}


def _disk_stat(path: Path):
    # os.stat is never patched by the VFS layer, so disk checks here stay
    # loop-free even while Path.exists/is_file/is_dir are patched
    try:
        return os.stat(path)
    except OSError:
        return None


def _disk_exists(path: Path) -> bool:
    return _disk_stat(path) is not None


def _disk_is_file(path: Path) -> bool:
    info = _disk_stat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def _disk_is_dir(path: Path) -> bool:
    info = _disk_stat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def glob_re(pattern: str) -> re.Pattern:
    """Compile a glob where * does not cross '/' and '**' does."""
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:[^/]+/)*")
                i += 3
            elif pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    compiled = re.compile("".join(out) + r"\Z")
    _GLOB_CACHE[pattern] = compiled
    return compiled


class EditSession:
    def __init__(self, files: list[Path]) -> None:
        self._files = sorted({self.canon(f) for f in files})
        self._staged: dict[Path, str | bytes | None] = {}

    # --- input set ---

    def files(self) -> list[Path]:
        return list(self._files)

    def glob(self, pattern: str) -> list[Path]:
        rx = glob_re(pattern)
        return [p for p in self._files if rx.match(self.relpath(p))]

    # --- overlay IO ---

    def read(self, path: str | Path) -> str | bytes:
        p = self.canon(path)
        content = self._staged.get(p, _MISSING)
        if content is not _MISSING:
            if content is None:
                raise FileNotFoundError(f"file is deleted in this session: {p}")
            return content
        return self.current(p)

    def write(self, path: str | Path, content: str | bytes) -> None:
        if not isinstance(content, (str, bytes)):
            raise TypeError(
                f"write() needs str or bytes, got {type(content).__name__}"
            )
        self._staged[self.canon(path)] = content

    def edit(self, path: str | Path, old: str, new: str, count: int = -1) -> int:
        text = self.read(path)
        n = text.count(old)
        if n == 0:
            raise ValueError(f"pattern not found in {self.canon(path)}: {old!r}")
        self.write(path, text.replace(old, new, count))
        return n if count < 0 else min(n, count)

    def delete(self, path: str | Path) -> None:
        p = self.canon(path)
        if p not in self._staged and not _disk_is_file(p):
            raise FileNotFoundError(f"no such file: {p}")
        self._staged[p] = None

    def rename(self, old: str | Path, new: str | Path) -> None:
        src = self.canon(old)
        if src not in self._staged and not _disk_is_file(src):
            raise FileNotFoundError(f"no such file: {src}")
        content = self.read(src)
        self._staged[src] = None
        self._staged[self.canon(new)] = content

    # --- engine ---

    def staged(self) -> dict[Path, str | bytes | None]:
        return dict(self._staged)

    def staged_content(self, path: str | Path) -> str | bytes | None:
        return self._staged.get(self.canon(path), _MISSING)

    def prune_unchanged(self) -> None:
        for path, content in list(self._staged.items()):
            if content is None or not _disk_is_file(path):
                continue
            try:
                if isinstance(content, bytes):
                    same = path.read_bytes() == content
                else:
                    same = path.read_text() == content
            except (OSError, UnicodeDecodeError):
                continue
            if same:
                del self._staged[path]

    # --- overlay-aware filesystem views (used by the stdlib patches) ---

    def current(self, path: str | Path) -> str | bytes:
        p = self.canon(path)
        content = self._staged.get(p, _MISSING)
        if content is not _MISSING and content is not None:
            return content
        if not _disk_is_file(p):
            raise FileNotFoundError(str(p))
        try:
            return p.read_text()
        except UnicodeDecodeError:
            return p.read_bytes()

    def exists(self, path: str | Path) -> bool:
        content = self.staged_content(path)
        if content is not _MISSING:
            return content is not None
        return _disk_exists(self.canon(path))

    def is_file(self, path: str | Path) -> bool:
        content = self.staged_content(path)
        if content is not _MISSING:
            return content is not None
        return _disk_is_file(self.canon(path))

    def is_dir(self, path: str | Path) -> bool:
        return _disk_is_dir(self.canon(path))

    def entries(self, directory: str | Path) -> list[Path]:
        """Merged directory listing: disk entries minus staged deletions,
        plus staged files created in this directory."""
        p = self.canon(directory)
        if not _disk_exists(p):
            raise FileNotFoundError(str(p))
        if not _disk_is_dir(p):
            raise NotADirectoryError(str(p))
        found: dict[str, Path] = {}
        with os.scandir(p) as scanner:
            for entry in scanner:
                if self._staged.get(self.canon(entry.path), _MISSING) is None:
                    continue
                found[entry.name] = Path(entry.path)
        for staged_path, content in self._staged.items():
            if content is None or staged_path.parent != p:
                continue
            found.setdefault(staged_path.name, staged_path)
        return [found[name] for name in sorted(found)]

    # --- helpers ---

    def relpath(self, path: str | Path) -> str:
        return display_path(self.canon(path))

    @staticmethod
    def canon(path: str | Path) -> Path:
        return Path(path).resolve()


def display_path(path: Path) -> str:
    cwd = Path.cwd()
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()
