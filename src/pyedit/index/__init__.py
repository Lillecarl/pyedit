"""The code index: definitions embedded as vectors, kept in a git ref,
searched by similarity. `build` embeds what the ref lacks; `matrix`
gathers the current files' vectors for `similar.search` and
`similar.duplicates`."""

from __future__ import annotations

import itertools
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from pyedit.index import embed, gitstore
from pyedit.index.chunks import NOT_DEFINITIONS, chunks, digest, indexable
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
CONCURRENCY = 4


@dataclass
class BuildStats:
    files: int = 0
    reused: int = 0
    embedded: int = 0
    """Files whose chunks the index did not have as a whole."""
    chunks: int = 0
    """Chunks sent to the embedder."""
    reused_chunks: int = 0
    """Chunks of those files whose hash the index already held."""
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
    concurrency: int = CONCURRENCY,
) -> BuildStats:
    """Embed what the index lacks for the files under `pattern`, and save
    it with every present file it has vectors for. A file gone from the
    tree, or changed, loses its old vectors."""
    repo = repo or gitstore.open_repo(session.root)
    existing = gitstore.load(repo, embedder.model, embedder.dimensions)
    # every vector the index holds, by its chunk's hash: a changed file
    # embeds only the definitions that changed
    pool: dict[str, np.ndarray] = {
        str(meta["hash"]): shard.vectors[row]
        for shard in existing.values()
        for row, meta in enumerate(shard.chunks)
        if "hash" in meta
    }
    stats = BuildStats()
    shards: dict[str, gitstore.Shard] = {}
    todo: dict[str, list[dict[str, Any]]] = {}
    fresh: dict[str, int] = {}
    """Chunk hash to its place in `texts`: one embedding per distinct text."""
    texts: list[str] = []
    wanted = set(session.glob(pattern))
    for path, key in _files(session, "**/*"):
        inside = path in wanted
        stats.files += inside
        if key in shards or key in todo:
            continue
        if key in existing:
            shards[key] = _with_hashes(session, path, existing[key])
            stats.reused += inside
            continue
        if not inside:
            continue
        metas: list[dict[str, Any]] = []
        for chunk in chunks(session, path):
            hashed = digest(chunk.text)
            if hashed in pool or hashed in fresh:
                stats.reused_chunks += 1
            else:
                fresh[hashed] = len(texts)
                texts.append(chunk.text)
            metas.append(
                {
                    "kind": chunk.kind,
                    "name": chunk.name,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "hash": hashed,
                }
            )
        todo[key] = metas
    batches = list(_batches(texts))
    starts = list(itertools.accumulate((len(b) for b in batches), initial=0))
    vectors: dict[int, list[float]] = {}

    def done(place: int, found: list[list[float]]) -> None:
        vectors.update(enumerate(found, start=starts[place]))
        if verbose:
            print(
                f"pyedit: index: embedded {len(vectors)}/{len(texts)} chunks",
                file=sys.stderr,
            )

    try:
        if batches:
            embed.embed_batches(embedder, batches, done, concurrency)
    finally:
        # a failed build saves every file it has all vectors for, so the
        # next one resumes
        for key, metas in todo.items():
            rows: list[np.ndarray | list[float]] = []
            for meta in metas:
                hashed = meta["hash"]
                if hashed in pool:
                    rows.append(pool[hashed])
                elif fresh[hashed] in vectors:
                    rows.append(vectors[fresh[hashed]])
                else:
                    break
            else:
                matrix = np.asarray(rows, dtype=np.float32)
                shards[key] = gitstore.Shard(
                    metas, matrix.reshape(len(metas), embedder.dimensions)
                )
                stats.embedded += 1
        stats.chunks = len(vectors)
        stats.pruned = len(set(existing) - set(shards))
        gitstore.save(repo, embedder.model, embedder.dimensions, shards)
    return stats


def _with_hashes(
    session: EditSession, path: Path, shard: gitstore.Shard
) -> gitstore.Shard:
    """A shard saved before chunks carried hashes, with them: the file is
    unchanged, so chunking it again names the same chunks in order."""
    if all("hash" in meta for meta in shard.chunks):
        return shard
    found = chunks(session, path)
    if [(c.start_line, c.end_line) for c in found] != [
        (m["start_line"], m["end_line"]) for m in shard.chunks
    ]:
        return shard
    metas = [
        {**meta, "hash": digest(chunk.text)}
        for meta, chunk in zip(shard.chunks, found, strict=True)
    ]
    return gitstore.Shard(metas, shard.vectors)


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
        # an index built before a kind left the chunker still holds it
        keep = [
            row
            for row, c in enumerate(shard.chunks)
            if c["kind"] not in NOT_DEFINITIONS
        ]
        rows.extend(
            Row(
                name,
                c["kind"],
                c["name"],
                c["start_line"],
                c["end_line"],
                c.get("hash"),
            )
            for c in (shard.chunks[row] for row in keep)
        )
        blocks.append(shard.vectors[keep])
    vectors = np.concatenate(blocks) if blocks else np.zeros((0, dimensions))
    return Gathered(Matrix.of(rows, vectors), stale)
