import pytest

from pyedit import rope
from pyedit.session import BudgetExceeded, EditSession


@pytest.fixture
def pkg(project):
    (project / "src" / "__init__.py").write_text("")
    (project / "src" / "mod.py").write_text("def foo():\n    return 1\n")
    (project / "app.py").write_text("from src.mod import foo\n\nprint(foo())\n")
    return project


def test_rename_stages_definition_and_references(pkg):
    session = EditSession()
    changed = session.rename_symbol("src/mod.py", 1, 5, "foo", "bar")
    assert pkg / "src" / "mod.py" in changed
    staged = session.staged()
    assert staged[pkg / "src" / "mod.py"] == "def bar():\n    return 1\n"
    assert staged[pkg / "app.py"] == "from src.mod import bar\n\nprint(bar())\n"


def test_rename_from_reference_position(pkg):
    session = EditSession()
    session.rename_symbol("app.py", 1, 20, "foo", "bar")
    assert "def bar():" in session.staged()[pkg / "src" / "mod.py"]


def test_rename_accepts_cursor_just_after_token(pkg):
    session = EditSession()
    session.rename_symbol("src/mod.py", 1, 7, "foo", "bar")
    assert "def bar():" in session.staged()[pkg / "src" / "mod.py"]


def test_rename_wrong_old_name_fails_loudly(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="resolves to 'foo'"):
        session.rename_symbol("src/mod.py", 1, 5, "wrong", "bar")
    # the failed selection left only the materialized read, no changes
    assert session.staged() == {
        pkg / "src" / "mod.py": "def foo():\n    return 1\n"
    }


def test_rename_rejects_non_identifiers(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="identifier"):
        session.rename_symbol("src/mod.py", 1, 5, "foo", "not-a-name")
    with pytest.raises(ValueError, match="identifier"):
        session.rename_symbol("src/mod.py", 1, 5, "foo", "123")


def test_rename_off_symbol_position_fails(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="no symbol to rename"):
        session.rename_symbol("src/mod.py", 2, 4, "foo", "bar")


def test_rename_line_out_of_range_fails(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="past the end"):
        session.rename_symbol("src/mod.py", 99, 0, "foo", "bar")


def test_rename_sees_staged_overlay_content(pkg):
    session = EditSession()
    session.edit("src/mod.py", "def foo():", "def foo():  # api\n")
    session.write("extra.py", "import src.mod\n\nsrc.mod.foo()\n")
    session.rename_symbol("src/mod.py", 1, 5, "foo", "bar")
    staged = session.staged()
    # the reference staged this run is renamed too: rope read it through the VFS
    assert staged[pkg / "extra.py"] == "import src.mod\n\nsrc.mod.bar()\n"
    assert "def bar():  # api\n" in staged[pkg / "src" / "mod.py"]


def test_module_rename_moves_file_and_fixes_importers(pkg):
    session = EditSession()
    changed = session.rename_module("src/mod.py", "mod", "module2")
    staged = session.staged()
    assert staged[pkg / "src" / "mod.py"] is None
    assert pkg / "src" / "module2.py" in changed
    assert staged[pkg / "app.py"] == "from src.module2 import foo\n\nprint(foo())\n"


def test_module_rename_wrong_old_name_fails(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="resolves to 'mod'"):
        session.rename_module("src/mod.py", "wrong", "module2")
    assert session.staged() == {}


def test_references_lists_definition_and_uses(pkg):
    session = EditSession()
    refs = session.references("app.py", 3, 6, "foo")
    located = {(r.path.name, r.line) for r in refs}
    assert ("mod.py", 1) in located
    assert ("app.py", 1) in located
    assert ("app.py", 3) in located


def test_references_wrong_name_fails(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="is 'foo', not 'bar'"):
        session.references("app.py", 3, 6, "bar")


def test_references_unknown_symbol(pkg):
    session = EditSession()
    with pytest.raises(ValueError, match="not 'nope'"):
        session.references("app.py", 3, 6, "nope")


def test_rename_twice_in_one_session(pkg):
    session = EditSession()
    session.rename_symbol("src/mod.py", 1, 5, "foo", "bar")
    session.rename_symbol("src/mod.py", 1, 5, "bar", "baz")
    staged = session.staged()
    assert "def baz():" in staged[pkg / "src" / "mod.py"]
    assert "print(baz())" in staged[pkg / "app.py"]


def test_rename_respects_budget(pkg):
    session = EditSession(max_files=1)
    with pytest.raises(BudgetExceeded):
        session.rename_symbol("src/mod.py", 1, 5, "foo", "bar")


def test_no_ropeproject_on_disk(pkg):
    session = EditSession()
    session.rename_symbol("src/mod.py", 1, 5, "foo", "bar")
    # after the rename the staged content spells the symbol 'bar'
    session.references("app.py", 3, 6, "bar")
    assert not (pkg / ".ropeproject").exists()


def test_external_reads_do_not_materialize(project):
    outside = project.parent / "outside.txt"
    outside.write_text("external\n")
    session = EditSession()
    assert session.read(outside) == "external\n"
    assert session.staged() == {}


def test_direct_lsp_module_call(pkg):
    session = EditSession()
    assert rope.rename_symbol(session, "src/mod.py", 1, 5, "foo", "bar") is not None
