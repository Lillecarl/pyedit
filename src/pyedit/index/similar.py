"""Nearest chunks by cosine similarity, brute force. Exact, and fast
enough at this scale: 30k chunks at 1024 dimensions is one matrix
product per query, about 10 ms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

BLOCK = 1024


@dataclass(frozen=True)
class Row:
    path: str
    kind: str
    name: str | None
    start_line: int
    end_line: int
    hash: str | None = None

    def contains(self, other: Row) -> bool:
        return (
            self.path == other.path
            and self.start_line <= other.start_line
            and other.end_line <= self.end_line
        )

    def overlaps(self, other: Row) -> bool:
        return (
            self.path == other.path
            and self.start_line <= other.end_line
            and other.start_line <= self.end_line
        )


@dataclass
class Matrix:
    rows: list[Row]
    vectors: np.ndarray
    """float32, one unit-length row per entry of `rows`."""

    @classmethod
    def of(cls, rows: list[Row], vectors: np.ndarray) -> Matrix:
        unit = vectors.astype(np.float32)
        norms = np.linalg.norm(unit, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls(rows, unit / norms)


def search(matrix: Matrix, query: Sequence[float], k: int) -> list[tuple[float, Row]]:
    if not matrix.rows:
        return []
    vector = np.asarray(query, dtype=np.float32)
    vector /= np.linalg.norm(vector) or 1.0
    scores = matrix.vectors @ vector
    top = np.argsort(-scores)[:k]
    return [(float(scores[i]), matrix.rows[i]) for i in top]


def duplicates(matrix: Matrix, threshold: float) -> list[tuple[float, Row, Row]]:
    """Pairs at or above `threshold`, best first. A pair inside one file
    whose spans overlap is the same code, not a copy. A pair whose both
    sides lie inside a kept pair's sides is part of that copy, so only the
    outer pair is reported."""
    rows = matrix.rows
    found: list[tuple[float, int, int]] = []
    for start in range(0, len(rows), BLOCK):
        block = matrix.vectors[start : start + BLOCK] @ matrix.vectors.T
        for local, j in zip(*np.nonzero(block >= threshold), strict=True):
            i = start + int(local)
            if int(j) > i and not rows[i].overlaps(rows[int(j)]):
                found.append((float(block[local, j]), i, int(j)))
    # outer pairs first, so a nested pair finds the pair that holds it
    found.sort(key=lambda f: -_span(rows[f[1]]) - _span(rows[f[2]]))
    kept: list[tuple[float, Row, Row]] = []
    for score, i, j in found:
        a, b = rows[i], rows[j]
        if any(
            (outer_a.contains(a) and outer_b.contains(b))
            or (outer_a.contains(b) and outer_b.contains(a))
            for _, outer_a, outer_b in kept
        ):
            continue
        kept.append((score, a, b))
    kept.sort(key=lambda pair: -pair[0])
    return kept


def _span(row: Row) -> int:
    return row.end_line - row.start_line
