import pytest

from pyedit.session import EditSession


@pytest.fixture
def session(project):
    return EditSession()


def test_glob_matches_relative_paths(session):
    assert [p.name for p in session.glob("src/*.py")] == ["a.py", "b.py"]
    assert [p.name for p in session.glob("**/*.py")] == ["a.py", "b.py"]
    assert session.glob("*.txt") == []


def test_glob_merges_staged_files(session, project):
    session.write("src/new.py", "x\n")
    session.delete("src/a.py")
    assert [p.name for p in session.glob("src/*.py")] == ["b.py", "new.py"]


def test_read_materializes_disk_content(session, project):
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_prune_drops_materialized_reads(session, project):
    session.read("src/a.py")
    session.prune_unchanged()
    assert session.staged() == {}


def test_read_sees_disk_content(session, project):
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"


def test_read_sees_staged_write(session, project):
    session.write("src/a.py", "rewritten\n")
    assert session.read("src/a.py") == "rewritten\n"


def test_write_accepts_str_and_bytes_only(session, project):
    session.write("src/a.py", b"bytes\n")
    assert session.read("src/a.py") == b"bytes\n"
    with pytest.raises(TypeError):
        session.write("src/a.py", 42)


def test_edit_replaces_and_counts(session, project):
    n = session.edit("src/a.py", "alpha", "ALPHA")
    assert n == 1
    assert session.read("src/a.py") == "ALPHA = 1\nbeta = 2\n"


def test_edit_missing_pattern_raises(session, project):
    with pytest.raises(ValueError):
        session.edit("src/a.py", "missing", "x")
    # the read materialized the original content; the edit staged nothing
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_delete_and_rename(session, project):
    session.rename("src/a.py", "src/c.py")
    staged = session.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "c.py"] == "alpha = 1\nbeta = 2\n"


def test_delete_missing_raises(session):
    with pytest.raises(FileNotFoundError):
        session.delete("src/nope.py")


def test_rename_missing_raises(session):
    with pytest.raises(FileNotFoundError):
        session.rename("src/nope.py", "src/x.py")


def test_read_after_delete_raises(session, project):
    session.delete("src/a.py")
    with pytest.raises(FileNotFoundError):
        session.read("src/a.py")


def test_prune_unchanged_drops_identical_writes(session, project):
    session.write("src/a.py", "alpha = 1\nbeta = 2\n")
    session.write("src/b.py", "changed\n")
    session.prune_unchanged()
    staged = session.staged()
    assert project / "src" / "a.py" not in staged
    assert staged[project / "src" / "b.py"] == "changed\n"


def test_new_file_staging(session, project):
    session.write("src/new.py", "fresh = 1\n")
    assert session.staged()[project / "src" / "new.py"] == "fresh = 1\n"
