"""The code index: definitions embedded as vectors, kept in a git ref,
searched by similarity. `build` embeds what the ref lacks; `matrix`
gathers the current files' vectors for `similar.search` and
`similar.duplicates`."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from pyedit.index import gitstore
from pyedit.index.chunks import chunks, indexable
from pyedit.index.similar import Matrix, Row

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import pygit2

    from pyedit.index.embed import Embedder
    from pyedit.session import EditSession

BATCH = 256
# characters per request: a provider quota counts tokens per request and
# per second (Nebius serving qwen3-embedding-8b answered 429 to 256
# chunks at once), and a token is about four characters
BATCH_CHARS = 100_000


@dataclass
class BuildStats:
    files: int = 0
    reused: int = 0
    embedded: int = 0
    chunks: int = 0
    pruned: int = 0


@dataclass
class Gathered:
    matrix: Matrix
    stale: list[str] = field(default_factory=list)
    """Files the index has no vectors for: changed or new since the build."""


def _files(session: EditSession, pattern: str) -> Iterator[tuple[Path, str]]:
    """Indexable text files under `pattern`, with their keys."""
    for path in session.glob(pattern):
        if session.is_link(path) or not indexable(path):
            continue
        content = session.read(path)
        if isinstance(content, bytes):
            continue
        yield path, gitstore.key_for(content.encode("utf-8"), path.suffix)


def build(
    session: EditSession,
    embedder: Embedder,
    *,
    pattern: str = "**/*",
    repo: pygit2.Repository | None = None,
    verbose: bool = False,
) -> BuildStats:
    """Embed what the index lacks for the files under `pattern`, and save
    it with every present file it has vectors for. A file gone from the
    tree, or changed, loses its old vectors."""
    repo = repo or gitstore.open_repo(session.root)
    existing = gitstore.load(repo, embedder.model, embedder.dimensions)
    stats = BuildStats()
    shards: dict[str, gitstore.Shard] = {}
    todo: dict[str, list[dict[str, object]]] = {}
    texts: list[str] = []
    wanted = set(session.glob(pattern))
    for path, key in _files(session, "**/*"):
        inside = path in wanted
        stats.files += inside
        if key in shards or key in todo:
            continue
        if key in existing:
            shards[key] = existing[key]
            stats.reused += inside
            continue
        if not inside:
            continue
        found = chunks(session, path)
        todo[key] = [
            {
                "kind": c.kind,
                "name": c.name,
                "start_line": c.start_line,
                "end_line": c.end_line,
            }
            for c in found
        ]
        texts.extend(c.text for c in found)
    vectors: list[list[float]] = []
    try:
        for batch in _batches(texts):
            vectors.extend(embedder.embed(batch))
            if verbose:
                print(
                    f"pyedit: index: embedded {len(vectors)}/{len(texts)} chunks",
                    file=sys.stderr,
                )
    finally:
        # a failed build saves what it embedded, so the next one resumes
        offset = 0
        for key, metas in todo.items():
            if offset + len(metas) > len(vectors):
                break
            rows = np.asarray(vectors[offset : offset + len(metas)], dtype=np.float32)
            shards[key] = gitstore.Shard(
                metas, rows.reshape(len(metas), embedder.dimensions)
            )
            offset += len(metas)
            stats.embedded += 1
        stats.chunks = offset
        stats.pruned = len(set(existing) - set(shards))
        gitstore.save(repo, embedder.model, embedder.dimensions, shards)
    return stats


def _batches(texts: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (len(batch) == BATCH or size + len(text) > BATCH_CHARS):
            yield batch
            batch = []
            size = 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


def gather(
    session: EditSession,
    model: str,
    dimensions: int,
    *,
    pattern: str = "**/*",
    repo: pygit2.Repository | None = None,
) -> Gathered:
    """The vectors of the current files under `pattern`, as the index
    holds them; files it lacks are `stale`."""
    repo = repo or gitstore.open_repo(session.root)
    shards = gitstore.load(repo, model, dimensions)
    rows: list[Row] = []
    blocks: list[np.ndarray] = []
    stale: list[str] = []
    for path, key in _files(session, pattern):
        shard = shards.get(key)
        if shard is None:
            stale.append(session.relpath(path))
            continue
        name = session.relpath(path)
        rows.extend(
            Row(name, c["kind"], c["name"], c["start_line"], c["end_line"])
            for c in shard.chunks
        )
        blocks.append(shard.vectors)
    vectors = np.concatenate(blocks) if blocks else np.zeros((0, dimensions))
    return Gathered(Matrix.of(rows, vectors), stale)
