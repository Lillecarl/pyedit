from pathlib import Path

import pytest

from pyedit import cli, vfs
from pyedit.session import EditSession


@pytest.fixture
def repo(project):
    (project / ".gitignore").write_text("*.log\nbuild/\n!keep.log\n")
    (project / "src" / "a.py").write_text("alpha = 1\n")
    (project / "src" / "b.log").write_text("log\n")
    (project / "src" / "keep.log").write_text("kept\n")
    (project / "src" / "build").mkdir()
    (project / "src" / "build" / "out.txt").write_text("out\n")
    return project


def test_session_glob_excludes_ignored(repo):
    session = EditSession()
    names = {p.name for p in session.glob("**/*")}
    assert names == {"a.py", "b.py", "keep.log", "note.txt"}


def test_negation_keeps_explicit(repo):
    session = EditSession()
    assert (repo / "src" / "keep.log") in session.glob("**/keep.log")


def test_dir_only_rule_excludes_contents(repo):
    session = EditSession()
    assert session.glob("src/build/*") == []


def test_nested_gitignore(repo):
    (repo / "src" / ".gitignore").write_text("c.py\n")
    (repo / "src" / "c.py").write_text("c\n")
    session = EditSession()
    names = {p.name for p in session.glob("src/*.py")}
    assert names == {"a.py", "b.py"}


def test_staged_new_files_are_filtered_too(repo):
    session = EditSession()
    session.write("src/new.log", "x\n")
    assert "new.log" not in {p.name for p in session.glob("src/*")}
    session.write("src/new.py", "y\n")
    assert "new.py" in {p.name for p in session.glob("src/*")}


def test_explicit_writes_to_ignored_paths_still_work(repo):
    session = EditSession()
    session.write("src/b.log", "rewritten\n")
    assert session.staged()[repo / "src" / "b.log"] == "rewritten\n"


def test_pathlib_glob_filtered(repo):
    session = EditSession()
    restore = vfs.install(session)
    try:
        names = {p.name for p in Path("src").glob("**/*")}
    finally:
        restore()
    assert names == {"a.py", "b.py", "keep.log"}


def test_no_gitignore_flag_disables(repo, capsys):
    script = repo / "edit.py"
    script.write_text(
        "import pyedit\n"
        "print(sorted(p.name for p in pyedit.glob('**/*')))\n"
    )
    assert cli.main(["--no-gitignore", "--script", str(script)]) == 0
    out = capsys.readouterr().out
    assert "b.log" in out and "out.txt" in out
