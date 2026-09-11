""".gitignore-aware filtering for discovery globs.

Rules come from every .gitignore between the working directory's
ancestors and each candidate's directory (nested files apply to their
own subtree). Matching is per-directory GitIgnoreSpec (pathspec
gitwildmatch). Only discovery is filtered; explicit reads and writes
work on ignored paths.

Approximation: negation rules only override rules within the same
.gitignore file; .git/info/exclude and the global excludesfile are not
consulted.
"""

from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec


class IgnoreFilter:
    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or Path.cwd()).resolve()
        self._specs: dict[Path, GitIgnoreSpec | None] = {}

    def ignored(self, path: Path, is_dir: bool = False) -> bool:
        path = Path(path).resolve()
        rel_suffix = "/" if is_dir else ""
        for base in self._chain(path):
            spec = self._spec(base)
            if spec is None:
                continue
            rel = path.relative_to(base).as_posix() + rel_suffix
            if spec.match_file(rel):
                return True
        return False

    def _chain(self, path: Path):
        """Every directory from the candidate up to the root that could
        hold a .gitignore governing it."""
        seen = set()
        current = path.parent
        while True:
            if current not in seen:
                seen.add(current)
                yield current
            if current == self._root or current.parent == current:
                return
            current = current.parent

    def _spec(self, base: Path) -> GitIgnoreSpec | None:
        if base not in self._specs:
            ignore_file = base / ".gitignore"
            if ignore_file.is_file():
                self._specs[base] = GitIgnoreSpec.from_lines(
                    ignore_file.read_text(encoding="utf-8").splitlines()
                )
            else:
                self._specs[base] = None
        return self._specs[base]
