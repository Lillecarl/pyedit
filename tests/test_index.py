"""The code index, against a fake embedder: no network, deterministic."""

from __future__ import annotations

import json
import re
import sys
import zlib
from typing import TYPE_CHECKING

import pygit2
import pytest

from pyedit import cli
from pyedit.index import build, embed, gather, gitstore, similar
from pyedit.session import EditSession

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
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
    (query,) = Bag().embed(["print a greeting message for a person"])
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


def test_cli_prints_duplicates_as_json(
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
    assert cli.main(["index", "duplicates", "--threshold", "0.8", *model]) == 0
    (pair,) = json.loads(capsys.readouterr().out)
    assert (pair["a"]["name"], pair["b"]["name"]) == ("double_sum", "twice_total")


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
    assert cli.main(["index", "duplicates"]) == 1
    assert "not inside a git repository" in capsys.readouterr().err
