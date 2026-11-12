"""ast-grep patterns over staged content: find_code and rewrite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pyedit.astgrep import PatternError, expand
from pyedit.session import EditSession

if TYPE_CHECKING:
    from pathlib import Path

    from pyedit.astgrep import CodeMatch


@pytest.fixture
def session(tmp_path: Path) -> EditSession:
    return EditSession(respect_gitignore=False, root=tmp_path)


def test_rewrite_keeps_the_separators_of_a_multi_match(session: EditSession):
    session.write("m.py", "print(a,  b)\nprint()\nx = 1\n")
    assert session.rewrite("m.py", "print($$$ARGS)", "log.info($$$ARGS)") == 2
    assert session.read("m.py") == "log.info(a,  b)\nlog.info()\nx = 1\n"


def test_find_code_sees_staged_content_and_binds_vars(
    session: EditSession, tmp_path: Path
):
    (tmp_path / "m.py").write_text("old = 1\n")
    session.write("m.py", "foo(1, bar)\n")
    (hit,) = session.find_code("m.py", "foo($A, $B)")
    assert (hit.path, hit.kind, hit.text) == ("m.py", "call", "foo(1, bar)")
    assert hit.vars == {"A": "1", "B": "bar"}


def test_columns_are_utf8_bytes_and_feed_splice(session: EditSession):
    session.write("m.py", 's = "åäö"; foo(1)\n')
    (hit,) = session.find_code("m.py", "foo($A)")
    assert (hit.start_line, hit.start_column) == (1, 14)
    session.splice(
        "m.py",
        [(hit.start_line, hit.start_column, hit.end_line, hit.end_column, "bar()")],
    )
    assert session.read("m.py") == 's = "åäö"; bar()\n'


def test_nested_matches_rewrite_the_outer_one(session: EditSession):
    session.write("m.py", "bar(foo(1))\n")
    assert session.rewrite("m.py", "$F($$$A)", "call($$$A)") == 1
    assert session.read("m.py") == "call(foo(1))\n"


def test_count_limits_replacements_per_file(session: EditSession):
    session.write("m.py", "f(1)\nf(2)\nf(3)\n")
    assert session.rewrite("m.py", "f($A)", "g($A)", count=1) == 1
    assert session.read("m.py") == "g(1)\nf(2)\nf(3)\n"


def test_rule_narrows_a_pattern(session: EditSession):
    session.write("m.py", "x == None\ndef f():\n    return y == None\n")
    rule = {"inside": {"kind": "function_definition", "stopBy": "end"}}
    assert session.rewrite("m.py", "$X == None", "$X is None", rule=rule) == 1
    assert session.read("m.py") == "x == None\ndef f():\n    return y is None\n"


def test_callable_fix_gets_the_match(session: EditSession):
    session.write("m.py", "f(1)\nf(22)\n")

    def double(hit: CodeMatch) -> str:
        return f"f({int(hit.vars['A']) * 2})"

    assert session.rewrite("m.py", "f($A)", double) == 2
    assert session.read("m.py") == "f(2)\nf(44)\n"


def test_glob_spans_files_and_skips_unknown_suffixes(session: EditSession):
    session.write("a.py", "f(1)\n")
    session.write("b.js", "f(2);\n")
    session.write("c.toml", "f = 3\n")
    hits = session.find_code("*", "f($A)")
    assert [(h.path, h.vars["A"]) for h in hits] == [("a.py", "1"), ("b.js", "2")]


def test_zero_matches_raise(session: EditSession):
    session.write("m.py", "x = 1\n")
    with pytest.raises(ValueError, match="no match for 'foo\\('"):
        session.rewrite("m.py", "foo(", "bar")


def test_bad_rule_raises_pattern_error_not_a_panic(session: EditSession):
    session.write("m.py", "x = 1\n")
    with pytest.raises(PatternError, match="ast-grep rejected"):
        session.find_code("m.py", rule={"kind": "no_such_kind"})


def test_unknown_language_raises_before_the_binding(session: EditSession):
    session.write("c.toml", "a = 1\n")
    with pytest.raises(PatternError, match=r"use pyedit\.query"):
        session.find_code("c.toml", "a = $V")
    with pytest.raises(PatternError, match="no language 'cobol'"):
        session.find_code("c.toml", "a = $V", lang="cobol")


def test_unbound_names_stay_literal_in_fix():
    assert expand("echo $HOME $A $$$B", {"A": "1", "B": "x, y"}) == "echo $HOME 1 x, y"


def test_rewrite_rides_the_session_gate(
    project: Path, capsys: pytest.CaptureFixture[str]
):
    from pyedit import cli

    script = project / "e.py"
    script.write_text("pyedit.rewrite('src/a.py', 'alpha = $V', 'alpha = ($V')\n")
    assert cli.main(["--apply", "--script", str(script)]) == 1
    err = capsys.readouterr().err
    assert "pyedit: syntax: src/a.py:" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"
