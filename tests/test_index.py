"""The code index, against a fake embedder: no network, deterministic."""

from __future__ import annotations

import json
import re
import sys
import zlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, override

import anyio
import pygit2
import pytest

from pyedit import cli, index
from pyedit.index import build, embed, gather, gitstore, similar, sql
from pyedit.index.chunks import chunks
from pyedit.session import EditSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    import apsw

    from pyedit.index.embed import Embed

DOUBLE = """def {name}(values):
    total = 0
    for value in values:
        total += value * 2
    return total
"""
GREET = """def greet(person):
    greeting = "hello"
    message = greeting + " " + person
    print(message)
"""
SHORT = "def tiny():\n    return 1\n"


class Bag:
    """Words hashed into buckets: shared words, near vectors."""

    model = "fake/bag"
    dimensions = 64

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.connections = 0

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Embed]:
        self.connections += 1
        yield self.embed

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.seen.extend(texts)
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in re.findall(r"\w+", text):
                vector[zlib.crc32(word.encode()) % self.dimensions] += 1.0
            vectors.append(vector)
        return vectors


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    (tmp_path / "a.py").write_text(DOUBLE.format(name="double_sum") + "\n" + SHORT)
    (tmp_path / "b.py").write_text(DOUBLE.format(name="twice_total"))
    (tmp_path / "c.py").write_text(GREET)
    (tmp_path / "data.json").write_text('{"a": {"b": 1, "c": 2, "d": 3, "e": 4}}\n')
    return pygit2.init_repository(str(tmp_path))


def session(root: Path) -> EditSession:
    return EditSession(root=root)


def test_build_embeds_definitions_and_reuses_them(
    tmp_path: Path, repo: pygit2.Repository
):
    bag = Bag()
    stats = build(session(tmp_path), bag, repo=repo)
    assert (stats.files, stats.embedded, stats.chunks) == (3, 3, 3)
    assert not any("tiny" in text for text in bag.seen)
    commit = repo[repo.references[gitstore.REF].target].peel(pygit2.Commit)
    assert commit.parents == []
    again = Bag()
    stats = build(session(tmp_path), again, repo=repo)
    assert (stats.reused, stats.embedded, again.seen) == (3, 0, [])


WRAPPED_CPP = """#ifndef DESK_H_
#define DESK_H_
namespace desk {
int Window::Twice(int value) {
  int total = value;
  return total * 2;
}
}  // namespace desk
#endif
"""
WRAPPED_RUST = """mod tests {
    enum Step {
        Moved {
            x: i32,
            y: i32,
        },
    }

    fn twice(value: i32) -> i32 {
        let total = value;
        total * 2
    }
}
"""


def test_wrappers_and_parts_are_no_chunks(tmp_path: Path):
    (tmp_path / "a.h").write_text(WRAPPED_CPP)
    (tmp_path / "b.rs").write_text(WRAPPED_RUST)
    here = session(tmp_path)
    assert [chunk.name for chunk in chunks(here, "a.h")] == ["Window::Twice"]
    assert [chunk.name for chunk in chunks(here, "b.rs")] == ["Step", "twice"]


def test_a_changed_file_alone_is_embedded_and_a_gone_one_pruned(
    tmp_path: Path, repo: pygit2.Repository
):
    build(session(tmp_path), Bag(), repo=repo)
    (tmp_path / "c.py").write_text(GREET.replace("hello", "goodbye"))
    (tmp_path / "b.py").unlink()
    bag = Bag()
    stats = build(session(tmp_path), bag, repo=repo)
    assert (stats.reused, stats.embedded, stats.pruned) == (1, 1, 2)
    assert len(bag.seen) == 1 and "goodbye" in bag.seen[0]


def test_a_narrow_build_keeps_the_rest_of_the_tree(
    tmp_path: Path, repo: pygit2.Repository
):
    build(session(tmp_path), Bag(), repo=repo)
    (tmp_path / "a.py").write_text(DOUBLE.format(name="halved_sum"))
    (tmp_path / "c.py").write_text(GREET.replace("hello", "goodbye"))
    bag = Bag()
    stats = build(session(tmp_path), bag, pattern="c.py", repo=repo)
    assert (stats.files, stats.embedded, stats.pruned) == (1, 1, 2)
    gathered = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo)
    assert gathered.stale == ["a.py"]
    assert sorted({row.path for row in gathered.matrix.rows}) == ["b.py", "c.py"]


def test_a_failed_build_keeps_what_it_embedded(
    tmp_path: Path, repo: pygit2.Repository, monkeypatch: pytest.MonkeyPatch
):
    class Quota(Bag):
        @override
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if self.seen:
                raise RuntimeError("429")
            return await super().embed(texts)

    monkeypatch.setattr(index, "BATCH", 1)
    with pytest.raises(RuntimeError, match="429"):
        build(session(tmp_path), Quota(), repo=repo)
    assert len(gitstore.load(repo, Bag.model, Bag.dimensions)) == 1
    bag = Bag()
    stats = build(session(tmp_path), bag, repo=repo)
    assert (stats.reused, stats.embedded, len(bag.seen)) == (1, 2, 2)


def test_batches_run_at_once_up_to_the_limit_and_keep_their_place(
    tmp_path: Path, repo: pygit2.Repository, monkeypatch: pytest.MonkeyPatch
):
    class Slow(Bag):
        running = 0
        peak = 0

        @override
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            self.running += 1
            self.peak = max(self.peak, self.running)
            # the first batch finishes last
            await anyio.sleep(0.2 if not self.seen else 0.01)
            self.running -= 1
            return await super().embed(texts)

    monkeypatch.setattr(index, "BATCH", 1)
    for number in range(4):
        (tmp_path / f"m{number}.py").write_text(
            GREET.replace("greeting", f"word{number}")
        )
    slow = Slow()
    build(session(tmp_path), slow, repo=repo, concurrency=3)
    assert (slow.peak, slow.connections, len(slow.seen)) == (3, 1, 7)
    built = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo)
    for row, vector in zip(built.matrix.rows, built.matrix.vectors, strict=True):
        (expected,) = embed.embed_once(Bag(), [source(tmp_path, row)])
        norm = sum(value * value for value in expected) ** 0.5
        assert vector.tolist() == pytest.approx([v / norm for v in expected])


def test_the_first_failure_is_raised_and_cancels_the_rest():
    class Refused(Bag):
        @override
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if texts == ["bad"]:
                raise RuntimeError("429")
            await anyio.sleep_forever()
            raise AssertionError

    heard: list[int] = []
    with pytest.raises(RuntimeError, match="429"):
        embed.embed_batches(
            Refused(), [["good"], ["bad"]], lambda place, _: heard.append(place), 2
        )
    assert heard == []


def source(root: Path, row: similar.Row) -> str:
    """The text of `row`, whose definition fills its lines."""
    lines = (root / row.path).read_text().splitlines()
    return "\n".join(lines[row.start_line - 1 : row.end_line])


def test_batches_stop_at_the_character_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(index, "BATCH_CHARS", 10)
    assert list(index._batches(["aaaa", "bbbb", "cccc", "d" * 20, "e"])) == [
        ["aaaa", "bbbb"],
        ["cccc"],
        ["d" * 20],
        ["e"],
    ]


def test_a_changed_file_embeds_only_its_changed_definitions(
    tmp_path: Path, repo: pygit2.Repository
):
    (tmp_path / "d.py").write_text(DOUBLE.format(name="sum_up") + "\n" + GREET)
    build(session(tmp_path), Bag(), repo=repo)
    (tmp_path / "d.py").write_text(
        DOUBLE.format(name="sum_up") + "\n" + GREET.replace("hello", "hi")
    )
    bag = Bag()
    stats = build(session(tmp_path), bag, repo=repo)
    assert (stats.embedded, stats.chunks, stats.reused_chunks) == (1, 1, 1)
    assert len(bag.seen) == 1 and '"hi"' in bag.seen[0]


def test_a_definition_in_two_files_is_embedded_once(
    tmp_path: Path, repo: pygit2.Repository
):
    (tmp_path / "d.py").write_text(GREET + "\nX = 1\n")
    bag = Bag()
    stats = build(session(tmp_path), bag, repo=repo)
    assert (stats.embedded, stats.chunks, stats.reused_chunks) == (4, 3, 1)
    assert bag.seen.count(GREET.rstrip("\n")) == 1


def test_shards_without_hashes_gain_them_without_embedding(
    tmp_path: Path, repo: pygit2.Repository
):
    build(session(tmp_path), Bag(), repo=repo)
    bare = {
        key: gitstore.Shard(
            [{k: v for k, v in m.items() if k != "hash"} for m in shard.chunks],
            shard.vectors,
        )
        for key, shard in gitstore.load(repo, Bag.model, Bag.dimensions).items()
    }
    gitstore.save(repo, Bag.model, Bag.dimensions, bare)
    bag = Bag()
    build(session(tmp_path), bag, repo=repo)
    assert bag.seen == []
    shards = gitstore.load(repo, Bag.model, Bag.dimensions).values()
    assert all("hash" in meta for shard in shards for meta in shard.chunks)


def test_call_site_arguments_are_not_chunks(tmp_path: Path):
    (tmp_path / "d.py").write_text(
        "run(\n    stores=[\n        1,\n        2,\n    ],\n)\n"
    )
    assert chunks(session(tmp_path), "d.py") == []


def test_gather_names_files_the_index_lacks(tmp_path: Path, repo: pygit2.Repository):
    build(session(tmp_path), Bag(), repo=repo)
    (tmp_path / "c.py").write_text(GREET.replace("hello", "goodbye"))
    gathered = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo)
    assert gathered.stale == ["c.py"]
    assert {row.path for row in gathered.matrix.rows} == {"a.py", "b.py"}


def test_duplicates_pairs_copies_across_files(tmp_path: Path, repo: pygit2.Repository):
    build(session(tmp_path), Bag(), repo=repo)
    matrix = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo).matrix
    pairs = similar.duplicates(matrix, 0.8)
    assert [(a.path, a.name, b.path, b.name) for _, a, b in pairs] == [
        ("a.py", "double_sum", "b.py", "twice_total")
    ]


def test_duplicates_skip_code_nested_in_its_own_match(
    tmp_path: Path, repo: pygit2.Repository
):
    method = "\n".join("    " + line for line in DOUBLE.format(name="run").splitlines())
    (tmp_path / "a.py").write_text("class Runner:\n" + method + "\n")
    (tmp_path / "b.py").write_text("class Walker:\n" + method + "\n")
    build(session(tmp_path), Bag(), repo=repo)
    matrix = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo).matrix
    pairs = similar.duplicates(matrix, 0.8)
    # the classes pair; their methods are inside that pair, and a class
    # never pairs with its own method
    assert [(a.name, b.name) for _, a, b in pairs] == [("Runner", "Walker")]


def test_search_ranks_the_described_definition_first(
    tmp_path: Path, repo: pygit2.Repository
):
    build(session(tmp_path), Bag(), repo=repo)
    matrix = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo).matrix
    (query,) = embed.embed_once(Bag(), ["print a greeting message for a person"])
    (score, top), *_ = similar.search(matrix, query, 3)
    assert (top.path, top.name) == ("c.py", "greet")
    assert score > 0


def test_another_models_vectors_survive_a_build(
    tmp_path: Path, repo: pygit2.Repository
):
    class Other(Bag):
        model = "fake/other"

    build(session(tmp_path), Other(), repo=repo)
    build(session(tmp_path), Bag(), repo=repo)
    assert len(gitstore.load(repo, Other.model, Other.dimensions)) == 3
    assert len(gitstore.load(repo, Bag.model, Bag.dimensions)) == 3


def database(tmp_path: Path, repo: pygit2.Repository) -> apsw.Connection:
    build(session(tmp_path), Bag(), repo=repo)
    gathered = gather(session(tmp_path), Bag.model, Bag.dimensions, repo=repo)
    return sql.connect(session(tmp_path), gathered, Bag)


def test_sql_clusters_group_every_copy(tmp_path: Path, repo: pygit2.Repository):
    (tmp_path / "d.py").write_text(DOUBLE.format(name="sum_twice"))
    db = database(tmp_path, repo)
    rows = sql.query(
        db,
        "SELECT c.cluster, k.name FROM clusters(0.8) c JOIN chunks k ON k.id = c.id "
        "ORDER BY c.cluster, k.name",
    )
    assert rows == [
        {"cluster": 1, "name": "double_sum"},
        {"cluster": 1, "name": "sum_twice"},
        {"cluster": 1, "name": "twice_total"},
    ]


def test_sql_ratio_compares_chunk_texts(tmp_path: Path, repo: pygit2.Repository):
    # a copy of double_sum under another name ranks above a different
    # function, whatever the embedding score says
    (tmp_path / "d.py").write_text(DOUBLE.format(name="sum_twice"))
    db = database(tmp_path, repo)
    rows = sql.query(
        db,
        "SELECT b.name, ratio(a.id, b.id) AS r FROM chunks a, chunks b "
        "WHERE a.name = 'double_sum' AND b.name IN ('sum_twice', 'greet') "
        "ORDER BY r DESC",
    )
    assert [row["name"] for row in rows] == ["sum_twice", "greet"]
    assert rows[0]["r"] > 0.9 > rows[1]["r"]
    assert sql.query(db, "SELECT ratio(0, 0) AS r") == [{"r": 1.0}]
    assert sql.query(db, "SELECT ratio(0, 999) AS r") == [{"r": None}]


def test_sql_neighbors_search_and_source(tmp_path: Path, repo: pygit2.Repository):
    db = database(tmp_path, repo)
    (first,) = sql.query(
        db,
        "SELECT k.name FROM neighbors((SELECT id FROM chunks WHERE name = 'double_sum'), 1) n "
        "JOIN chunks k ON k.id = n.id",
    )
    assert first == {"name": "twice_total"}
    (top,) = sql.query(
        db,
        "SELECT k.path, source(k.id) AS text FROM search('print a greeting message', 1) s "
        "JOIN chunks k ON k.id = s.id",
    )
    assert top == {"path": "c.py", "text": GREET.rstrip("\n")}
    hashes = sql.query(db, "SELECT count(DISTINCT hash) AS n FROM chunks")
    assert hashes == [{"n": 3}]


def test_cli_answers_sql_as_json(
    tmp_path: Path,
    repo: pygit2.Repository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)

    def fake(*args: object) -> Bag:
        return Bag()

    monkeypatch.setattr(embed, "hosted", fake)
    model = ["--model", Bag.model, "--dimensions", str(Bag.dimensions)]
    assert cli.main(["index", "build", *model]) == 0
    assert json.loads(capsys.readouterr().out)["embedded"] == 3
    query = (
        "SELECT a.name AS a, b.name AS b FROM pairs(0.8) p "
        "JOIN chunks a ON a.id = p.a JOIN chunks b ON b.id = p.b"
    )
    assert cli.main(["index", "sql", query, *model]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"a": "double_sum", "b": "twice_total"}
    ]
    assert cli.main(["index", "sql", "SELECT nope FROM chunks", *model]) == 1
    assert "no such column: nope" in capsys.readouterr().err


def test_cli_without_aid_models_says_so(
    tmp_path: Path,
    repo: pygit2.Repository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "aid_models.embedding", None)
    assert cli.main(["index", "build"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "built without it" in captured.err


def test_outside_a_repository_the_cli_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["index", "sql", "SELECT 1"]) == 1
    assert "not inside a git repository" in capsys.readouterr().err
