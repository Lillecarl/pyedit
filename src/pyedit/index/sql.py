"""SQL over the code index: one in-memory SQLite database per query
(apsw), so a question is a query, not a new flag.

    chunks(id, path, kind, name, start_line, end_line, hash)
    pairs(threshold)       -> a, b, score       similar chunks, `similar.duplicates`' rules
    clusters(threshold)    -> cluster, id, score  pairs joined into groups; 1 is the best
    neighbors(chunk, k)    -> id, score         nearest chunks to one chunk
    search(text, k)        -> id, score         nearest chunks to a description (embeds it)
    source(id)                                  the chunk's text from the working tree

An id holds for one query only. Table-valued functions take their
arguments in the FROM clause: `SELECT * FROM pairs(0.95)`.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from pyedit.index import similar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import apsw

    from pyedit.index import Gathered
    from pyedit.index.embed import Embedder
    from pyedit.session import EditSession


def connect(
    session: EditSession,
    gathered: Gathered,
    embedder: Callable[[], Embedder] | None = None,
) -> apsw.Connection:
    """A database over `gathered`. `embedder` is called once, on the first
    `search`; without it `search` raises."""
    import apsw
    import apsw.ext

    db = apsw.Connection(":memory:")
    rows = gathered.matrix.rows
    ids = {row: number for number, row in enumerate(rows)}
    db.execute(
        "CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, "
        "name TEXT, start_line INTEGER, end_line INTEGER, hash TEXT)"
    )
    with db:
        db.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (n, r.path, r.kind, r.name, r.start_line, r.end_line, r.hash)
                for n, r in enumerate(rows)
            ],
        )
    found: dict[float, list[tuple[int, int, float]]] = {}

    def scored(threshold: float) -> list[tuple[int, int, float]]:
        if threshold not in found:
            found[threshold] = [
                (ids[a], ids[b], score)
                for score, a, b in similar.duplicates(gathered.matrix, threshold)
            ]
        return found[threshold]

    def pairs(threshold: float = 0.9) -> list[tuple[int, int, float]]:
        return scored(threshold)

    def clusters(threshold: float = 0.9) -> list[tuple[int, int, float]]:
        return _clusters(scored(threshold))

    # not `id`: apsw refuses a parameter named like an output column
    def neighbors(chunk: int, k: int = 10) -> Iterable[tuple[int, float]]:
        row = rows[chunk]
        hits = similar.search(
            gathered.matrix, gathered.matrix.vectors[chunk], len(rows)
        )
        kept = ((ids[other], score) for score, other in hits if not row.overlaps(other))
        return itertools.islice(kept, k)

    held: list[Embedder] = []

    def search(text: str, k: int = 10) -> list[tuple[int, float]]:
        from pyedit.index.embed import QUERY_PREFIX

        if embedder is None:
            raise ValueError("search needs an embedder: aid-models and an API key")
        if not held:
            held.append(embedder())
        (vector,) = held[0].embed([QUERY_PREFIX + text])
        return [
            (ids[row], score)
            for score, row in similar.search(gathered.matrix, vector, k)
        ]

    tables: dict[str, tuple[Callable[..., Any], tuple[str, ...]]] = {
        "pairs": (pairs, ("a", "b", "score")),
        "clusters": (clusters, ("cluster", "id", "score")),
        "neighbors": (neighbors, ("id", "score")),
        "search": (search, ("id", "score")),
    }
    for name, (function, columns) in tables.items():
        # make_virtual_module reads these two attributes off the function
        vars(function).update(
            columns=columns, column_access=apsw.ext.VTColumnAccess.By_Index
        )
        apsw.ext.make_virtual_module(db, name, function)

    def source(*args: apsw.SQLiteValue) -> apsw.SQLiteValue:
        (chunk,) = args
        if not isinstance(chunk, int) or not 0 <= chunk < len(rows):
            return None
        row = rows[chunk]
        text = session.read(row.path)
        if isinstance(text, bytes):
            return None
        return "\n".join(text.split("\n")[row.start_line - 1 : row.end_line])

    db.create_scalar_function("source", source, 1, deterministic=True)
    return db


def query(db: apsw.Connection, sql: str) -> list[dict[str, Any]]:
    """Every row of the last statement, as column -> value."""
    cursor = db.cursor()
    out: list[dict[str, Any]] = []
    for values in cursor.execute(sql):
        names = [column for column, _ in cursor.get_description()]
        out.append(dict(zip(names, values, strict=True)))
    return out


def _clusters(edges: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Connected chunks as groups, best group first; each member scored by
    its best edge."""
    parent: dict[int, int] = {}

    def root(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    best: dict[int, float] = {}
    for a, b, score in edges:
        parent[root(a)] = root(b)
        best[a] = max(best.get(a, 0.0), score)
        best[b] = max(best.get(b, 0.0), score)
    groups: dict[int, list[int]] = {}
    for node in best:
        groups.setdefault(root(node), []).append(node)
    ordered = sorted(
        groups.values(),
        key=lambda members: (-max(best[m] for m in members), min(members)),
    )
    return [
        (number, member, best[member])
        for number, members in enumerate(ordered, start=1)
        for member in sorted(members)
    ]
