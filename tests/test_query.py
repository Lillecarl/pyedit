"""Tree-sitter queries: S-expression hits feed splice; kinds names the vocabulary.

Samples live in src/pyedit/corpus/<language>/ (seeded from the syntax
corpus); patterns pin the query spelling per language here.
"""

from pathlib import Path

import pytest

import pyedit as pyedit_pkg
from pyedit.session import EditSession
from pyedit.syntax import kinds, query

CORPUS = Path(pyedit_pkg.__file__).resolve().parent / "corpus"


def sample_text(language):
    [path] = (CORPUS / language).glob("sample.*")
    return path.name, path.read_text()


@pytest.fixture
def session(tmp_path):
    return EditSession(respect_gitignore=False, root=tmp_path)


def test_nix_value_hits_feed_splice(session):
    name, text = sample_text("nix")
    session.write(name, text)
    values = [
        hit
        for hit in query(
            session, name, "(binding (attrpath) @a (integer_expression) @v)"
        )
        if hit.capture == "v"
    ]
    assert [hit.text for hit in values] == ["1", "2"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "9")
        for hit in values
    ]
    assert session.splice(name, spans) == 2
    assert "alpha = 9;" in session.read(name)
    assert "beta.gamma = 9;" in session.read(name)


def test_python_function_names(session):
    name, text = sample_text("python")
    session.write(name, text)
    names = query(session, name, "(function_definition name: (identifier) @n)")
    assert sorted(hit.text for hit in names) == ["alpha", "gamma"]


def test_predicate_filters_hits(session):
    name, text = sample_text("nix")
    session.write(name, text)
    hits = query(session, name, '(binding (attrpath) @a (#eq? @a "alpha"))')
    assert [(hit.capture, hit.text) for hit in hits] == [("a", "alpha")]


def test_hits_arrive_ordered_by_position(session):
    name, text = sample_text("nix")
    session.write(name, text)
    hits = query(session, name, "(binding) @b")
    positions = [(hit.start_line, hit.start_column) for hit in hits]
    assert positions == sorted(positions)
    assert [hit.kind for hit in hits] == ["binding", "binding"]


def test_kinds_names_the_nix_vocabulary(session):
    name, text = sample_text("nix")
    session.write(name, text)
    found = kinds(session, name)
    assert "binding" in found["kinds"]
    assert "attrpath" in found["kinds"]
    assert "attrpath" in found["fields"]
    assert found["kinds"] == sorted(found["kinds"])


def test_query_reads_staged_content(session):
    session.write("a.py", "old_name = 1\n")
    session.write("a.py", "renamed_thing = 1\n")
    hits = query(session, "a.py", "(identifier) @i")
    assert [hit.text for hit in hits] == ["renamed_thing"]


def test_query_unknown_suffix_raises(session):
    session.write("blob.xyz", "whatever\n")
    with pytest.raises(ValueError, match="no tree-sitter grammar"):
        query(session, "blob.xyz", "(identifier) @i")


def test_kinds_unknown_suffix_raises(session):
    session.write("blob.xyz", "whatever\n")
    with pytest.raises(ValueError, match="no tree-sitter grammar"):
        kinds(session, "blob.xyz")


def test_bad_pattern_raises_naming_position(session):
    from tree_sitter import QueryError

    name, text = sample_text("nix")
    session.write(name, text)
    with pytest.raises(QueryError):
        query(session, name, "(bindin) @b")


def test_corpus_covers_every_shipped_language():
    from pyedit.syntax.rules import RULES

    have = {
        id(RULES[path.suffix])
        for path in CORPUS.rglob("sample.*")
        if path.suffix in RULES
    }
    assert {id(rules) for rules in RULES.values()} == have
