"""In-memory write overlay for edit scripts.

The session is injected into edit scripts as the global name ``pyedit``.
Every write lands in memory; disk is touched only when the caller applies
the staged changes. Reads see staged content, so scripts get
read-your-writes semantics.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


class EditSession:
    def __init__(self, files: list[Path]) -> None:
        self._files = sorted({Path(f).resolve() for f in files})
        self._staged: dict[Path, str | None] = {}

    # --- input set ---

    def files(self) -> list[Path]:
        return list(self._files)

    def glob(self, pattern: str) -> list[Path]:
        return [p for p in self._files if fnmatch.fnmatch(self.relpath(p), pattern)]

    # --- overlay IO ---

    def read(self, path: str | Path) -> str:
        p = self._canon(path)
        if p in self._staged:
            content = self._staged[p]
            if content is None:
                raise FileNotFoundError(f"file is deleted in this session: {p}")
            return content
        return p.read_text()

    def write(self, path: str | Path, content: str) -> None:
        if not isinstance(content, str):
            raise TypeError(f"write() needs str, got {type(content).__name__}")
        self._staged[self._canon(path)] = content

    def edit(self, path: str | Path, old: str, new: str, count: int = -1) -> int:
        text = self.read(path)
        n = text.count(old)
        if n == 0:
            raise ValueError(f"pattern not found in {self._canon(path)}: {old!r}")
        self.write(path, text.replace(old, new, count))
        return n if count < 0 else min(n, count)

    def delete(self, path: str | Path) -> None:
        p = self._canon(path)
        if p not in self._staged and not p.is_file():
            raise FileNotFoundError(f"no such file: {p}")
        self._staged[p] = None

    def rename(self, old: str | Path, new: str | Path) -> None:
        src = self._canon(old)
        if src not in self._staged and not src.is_file():
            raise FileNotFoundError(f"no such file: {src}")
        content = self.read(src)
        self._staged[src] = None
        self._staged[self._canon(new)] = content

    # --- engine ---

    def staged(self) -> dict[Path, str | None]:
        return dict(self._staged)

    def prune_unchanged(self) -> None:
        unchanged = [
            p
            for p, content in self._staged.items()
            if p.is_file() and p.read_text() == content
        ]
        for p in unchanged:
            del self._staged[p]

    # --- helpers ---

    def relpath(self, path: str | Path) -> str:
        return display_path(self._canon(path))

    @staticmethod
    def _canon(path: str | Path) -> Path:
        return Path(path).resolve()


def display_path(path: Path) -> str:
    cwd = Path.cwd()
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()
