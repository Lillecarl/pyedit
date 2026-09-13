"""Unified-diff hunk renderer over in-memory git objects.

`DiffRenderer.hunks()` returns the `@@` hunk body for two texts - git
conventions, including the ``\\ No newline at end of file`` markers for
unterminated content. Only the hunk body: callers assemble their own
file headers.

Diffing reads straight from the blobs' data pointers (git_diff_blobs),
so the store those blobs live in is the only state libgit2 needs.
`pyedit.memgit` is that store, and its docstring holds the rules it
obeys. The store is reset per call, so nothing accumulates.
"""

from __future__ import annotations

from pyedit.memgit import MemoryRepo


class DiffRenderer:
    def __init__(self) -> None:
        self._repo = None

    def hunks(self, a_text: str, b_text: str, context: int = 3) -> str:
        repo = self._repository()
        repo.reset()
        body = (
            repo.blob(a_text).diff(repo.blob(b_text), context_lines=context).text or ""
        )
        lines = body.splitlines(keepends=True)
        index = 0
        while index < len(lines) and not lines[index].startswith("@@"):
            index += 1
        return "".join(lines[index:])

    def _repository(self) -> MemoryRepo:
        if self._repo is None:
            self._repo = MemoryRepo()
        return self._repo
