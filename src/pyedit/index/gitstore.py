"""Where the index lives: one commit with no history on `refs/pyedit/index`.

    v1/<model>/<dimensions>/<key[:2]>/<key>.json   the chunks: kind, name, lines, hash
    v1/<model>/<dimensions>/<key[:2]>/<key>.f16    their vectors, little-endian float16 rows

A key is a file's git blob id plus its suffix. An unchanged file has the
same key on every branch, so its vectors are computed once and git stores
them once. A chunk's hash lets a changed file keep the vectors of its
unchanged definitions. A ref outside refs/heads is not fetched by default: only who
asks for the index downloads it (`git fetch origin refs/pyedit/index`).
Each save replaces the commit, so vectors of files no longer present
become garbage git collects. Other models' trees are kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pygit2

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

REF = "refs/pyedit/index"
FORMAT = "v1"
_BLOB = pygit2.enums.FileMode.BLOB
_TREE = pygit2.enums.FileMode.TREE


class IndexStoreError(RuntimeError):
    pass


@dataclass
class Shard:
    """One file's chunks and their vectors, row for row."""

    chunks: list[dict[str, Any]]
    vectors: np.ndarray


def key_for(content: bytes, suffix: str) -> str:
    return f"{pygit2.hash(content)}{suffix}"


def model_dir(model: str, dimensions: int) -> str:
    return f"{FORMAT}/{model}/{dimensions}"


def open_repo(root: Path) -> pygit2.Repository:
    found = pygit2.discover_repository(str(root))
    if found is None:
        raise IndexStoreError(
            f"{root} is not inside a git repository; the index lives in the git ref {REF}"
        )
    return pygit2.Repository(found)


def load(repo: pygit2.Repository, model: str, dimensions: int) -> dict[str, Shard]:
    """Every shard of one model, by key. No index yet: none."""
    ref = repo.references.get(REF)
    if ref is None:
        return {}
    tree = repo[ref.target].peel(pygit2.Tree)
    try:
        top = repo[tree[model_dir(model, dimensions)].id].peel(pygit2.Tree)
    except KeyError:
        return {}
    shards: dict[str, Shard] = {}
    for fan in top:
        fan_tree = repo[fan.id].peel(pygit2.Tree)
        for entry in fan_tree:
            name = _name(entry)
            if not name.endswith(".json"):
                continue
            key = name.removesuffix(".json")
            chunks = json.loads(repo[entry.id].peel(pygit2.Blob).data)
            raw = repo[fan_tree[f"{key}.f16"].id].peel(pygit2.Blob).data
            vectors = np.frombuffer(raw, dtype="<f2").reshape(len(chunks), dimensions)
            shards[key] = Shard(chunks, vectors)
    return shards


def save(
    repo: pygit2.Repository, model: str, dimensions: int, shards: Mapping[str, Shard]
) -> pygit2.Oid:
    """Replace one model's shards with `shards` in a new commit and point
    the ref at it."""
    prefix = model_dir(model, dimensions) + "/"
    ref = repo.references.get(REF)
    kept = () if ref is None else _walk(repo, repo[ref.target].peel(pygit2.Tree), "")
    flat = {path: oid for path, oid in kept if not path.startswith(prefix)}
    for key, shard in shards.items():
        if shard.vectors.shape != (len(shard.chunks), dimensions):
            raise IndexStoreError(
                f"{key}: {len(shard.chunks)} chunks but vectors of shape {shard.vectors.shape}"
            )
        base = f"{prefix}{key[:2]}/{key}"
        flat[f"{base}.json"] = repo.create_blob(json.dumps(shard.chunks).encode())
        flat[f"{base}.f16"] = repo.create_blob(shard.vectors.astype("<f2").tobytes())
    tree = _build(repo, flat)
    signature = _signature(repo)
    commit = repo.create_commit(
        None, signature, signature, f"pyedit index: {model} at {dimensions}", tree, []
    )
    repo.references.create(REF, commit, force=True)
    return commit


def _walk(
    repo: pygit2.Repository, tree: pygit2.Tree, prefix: str
) -> Iterator[tuple[str, pygit2.Oid]]:
    for entry in tree:
        path = prefix + _name(entry)
        if entry.filemode == _TREE:
            yield from _walk(repo, repo[entry.id].peel(pygit2.Tree), path + "/")
        else:
            yield path, entry.id


def _build(repo: pygit2.Repository, flat: Mapping[str, pygit2.Oid]) -> pygit2.Oid:
    nested: dict[str, Any] = {}
    for path, oid in flat.items():
        *dirs, name = path.split("/")
        node = nested
        for part in dirs:
            node = node.setdefault(part, {})
        node[name] = oid

    def write(node: dict[str, Any]) -> pygit2.Oid:
        builder = repo.TreeBuilder()
        for name, value in sorted(node.items()):
            if isinstance(value, dict):
                builder.insert(name, write(value), _TREE)
            else:
                builder.insert(name, value, _BLOB)
        return builder.write()

    return write(nested)


def _name(entry: pygit2.Object) -> str:
    # pygit2 types a tree entry's name optional; one read from a tree has one
    if entry.name is None:
        raise IndexStoreError(f"{REF}: a tree entry without a name ({entry.id})")
    return entry.name


def _signature(repo: pygit2.Repository) -> pygit2.Signature:
    try:
        return repo.default_signature
    except (KeyError, pygit2.GitError):
        return pygit2.Signature("pyedit", "pyedit@localhost")
