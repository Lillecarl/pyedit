"""The syntax package: position queries, outlines, problem checks.

Grammars come from the nix environment; every tree-sitter parse here
is in-process.
"""

import pytest

from pyedit.session import EditSession
from pyedit.syntax import (
    SyntaxProblem,
    _parser_for,
    outline,
    node_at,
    problems,
    render,
)
from pyedit.syntax.rules import RULES


@pytest.fixture
def session(tmp_path):
    return EditSession(respect_gitignore=False, root=tmp_path)


PYTHON_TEXT = (
    "def alpha(x, y=1):\n"
    "    return x + y\n"
    "\n"
    "class Beta:\n"
    "    attr = 2\n"
    "    def gamma(self):\n"
    "        return self.attr\n"
)


def test_node_at_returns_the_identifier_and_enclosing_def(session):
    session.write("a.py", PYTHON_TEXT)
    info = node_at(session, "a.py", 1, 5)
    assert info.kind == "identifier"
    assert info.name == "alpha"
    assert info.start_line == 1
    assert info.start_column == 4
    assert info.enclosing.kind == "function_definition"
    assert info.enclosing.name == "alpha"
    assert info.enclosing.end_line == 2


def test_node_at_enclosing_finds_the_method(session):
    session.write("a.py", PYTHON_TEXT)
    info = node_at(session, "a.py", 6, 9)
    assert info.kind == "identifier"
    assert info.name == "gamma"
    assert info.enclosing.kind == "function_definition"
    assert info.enclosing.name == "gamma"


def test_node_at_reads_staged_content(session):
    session.write("a.py", "old_name = 1\n")
    session.write("a.py", "renamed_thing = 1\n")
    info = node_at(session, "a.py", 1, 1)
    assert info.name == "renamed_thing"


def test_node_at_accepts_the_column_just_after_a_token(session):
    session.write("a.py", "value = 1\n")
    assert node_at(session, "a.py", 1, 5).name == "value"


def test_node_at_columns_are_characters_not_bytes(session):
    session.write("u.py", 'label = "𝐀"\n')
    info = node_at(session, "u.py", 1, 9)  # inside the multibyte char
    assert info.kind == "string_content"
    assert info.text == "𝐀"
    assert info.start_column == 9  # byte columns would say 13
    assert info.end_column == 10  # byte columns would say 17


def test_node_at_bounds_are_checked(session):
    session.write("a.py", "one_line\n")
    with pytest.raises(ValueError, match="past the end of line 1"):
        node_at(session, "a.py", 1, 99)
    with pytest.raises(ValueError, match="past the end of the file"):
        node_at(session, "a.py", 9, 0)


def test_node_at_unknown_suffix_raises(session):
    session.write("blob.xyz", "whatever\n")
    with pytest.raises(ValueError, match="no tree-sitter grammar"):
        node_at(session, "blob.xyz", 1, 0)


def test_outline_lists_python_definitions_without_noise(session):
    session.write("a.py", PYTHON_TEXT)
    entries = outline(session, "a.py")
    by_name = {entry.name: entry for entry in entries}
    assert set(by_name) == {"alpha", "Beta", "gamma"}
    assert by_name["gamma"].kind == "function_definition"
    assert by_name["gamma"].start_line == 6


def test_outline_skips_imports_and_default_parameters(session):
    session.write("a.py", "import os\nimport x as y\ndef f(a, b=1):\n    pass\n")
    names = [entry.name for entry in outline(session, "a.py")]
    assert names == ["f"]


def test_outline_spans_sort_by_position(session):
    session.write("u.py", 'label = "𝐀"\ndef later():\n    pass\n')
    entries = outline(session, "u.py")
    assert [entry.name for entry in entries] == ["later"]
    assert entries[0].start_line == 2
    assert entries[0].start_column == 0  # the def keyword starts the span


def test_outline_nix_uses_attrpath_titles(session):
    session.write("cfg.nix", "{\n  alpha = 1;\n  beta.gamma = 2;\n}\n")
    entries = outline(session, "cfg.nix")
    assert [(entry.kind, entry.name) for entry in entries] == [
        ("binding", "alpha"),
        ("binding", "beta.gamma"),
    ]


def test_outline_go_finds_functions(session):
    session.write(
        "m.go",
        "package main\n\nfunc Alpha(x int) int {\n\treturn x\n}\n\ntype Beta struct {\n\tgamma int\n}\n",
    )
    names = [entry.name for entry in outline(session, "m.go")]
    assert "Alpha" in names
    assert "Beta" in names
    assert "gamma" not in names  # struct fields are noise


def test_problems_python_uses_compile_messages(session):
    session.write("bad.py", "def broken(:\n    pass\n")
    found = problems(session, "bad.py")
    assert len(found) == 1
    assert found[0].line == 1
    assert found[0].message


def test_problems_clean_python_is_empty(session):
    session.write("a.py", PYTHON_TEXT)
    assert problems(session, "a.py") == []


def test_problems_go_uses_tree_sitter(session):
    session.write("m.go", "func broken(:\n}\n")
    found = problems(session, "m.go")
    assert found
    assert found[0].line == 1
    assert "syntax error" in found[0].message or "missing" in found[0].message


def test_problems_unknown_language_silently_skips(session):
    session.write("blob.xyz", "def broken(:\n")
    assert problems(session, "blob.xyz") == []


def test_every_shipped_grammar_loads():
    # the nix environment ships all of them; a dead grammar here means
    # the dependency set or the loader regressed
    dead = [suffix for suffix in sorted(RULES) if _parser_for(suffix) is None]
    assert dead == []


def test_render_spans_and_carets():
    block = render(
        SyntaxProblem(line=1, column=4, end_column=6, message="x"), "def (:\n"
    )
    assert block.splitlines()[0] == "   1 | def (:"
    assert block.splitlines()[1].endswith("     ^^")

    single = render(SyntaxProblem(line=1, column=2, message="x"), "abc\n")
    assert single.splitlines()[1].endswith("   ^")


def test_python_problem_carries_the_span():
    class S:
        def read(self, path):
            return "if = 1\n"

    (problem,) = problems(S(), "x.py")
    assert problem.line == 1 and problem.column == 3
    assert problem.end_column == 4
