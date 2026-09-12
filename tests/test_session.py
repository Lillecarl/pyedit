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


def test_relative_paths_resolve_against_root(tmp_path):
    # no chdir: the cwd is deliberately elsewhere
    (tmp_path / "a.txt").write_text("x\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("s\n")
    session = EditSession(respect_gitignore=False, root=tmp_path)
    assert [p.name for p in session.glob("**/*.txt")] == ["a.txt", "b.txt"]
    session.write("b.txt", "y\n")
    session.edit("sub/b.txt", "s", "S")
    assert session.read("b.txt") == "y\n"
    assert session.read("sub/b.txt") == "S\n"
    # staged content only; disk truth is untouched
    assert (tmp_path / "sub" / "b.txt").read_text() == "s\n"


def test_edit_replaces_and_counts(session, project):
    n = session.edit("src/a.py", "alpha", "ALPHA")
    assert n == 1
    assert session.read("src/a.py") == "ALPHA = 1\nbeta = 2\n"


def test_edit_missing_pattern_raises(session, project):
    with pytest.raises(ValueError):
        session.edit("src/a.py", "missing", "x")
    # the read materialized the original content; the edit staged nothing
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_edit_empty_pattern_raises(session, project):
    # "" counts len+1 times and replaces at every position: without the
    # guard it splices the replacement between every character
    with pytest.raises(ValueError):
        session.edit("src/a.py", "", "x")
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"


def test_edit_range_scopes_duplicates(session, project):
    session.write("dup.txt", "x = 1\nx = 1\nx = 1\n")
    n = session.edit("dup.txt", "x = 1", "x = 2", start_line=2, stop_line=2)
    assert n == 1
    assert session.read("dup.txt") == "x = 1\nx = 2\nx = 1\n"


def test_edit_range_is_inclusive_and_spans_lines(session, project):
    session.write("multi.txt", "a\nb\nc\nd\n")
    n = session.edit("multi.txt", "b\nc", "B\nC", start_line=2, stop_line=3)
    assert n == 1
    assert session.read("multi.txt") == "a\nB\nC\nd\n"


def test_edit_match_crossing_boundary_needs_the_newline_in_range(session, project):
    session.write("bound.txt", "ab\nab\n")
    with pytest.raises(ValueError):
        session.edit("bound.txt", "b\na", "X", start_line=1, stop_line=1)
    n = session.edit("bound.txt", "b\na", "X", start_line=1, stop_line=2)
    assert n == 1
    assert session.read("bound.txt") == "aXb\n"


def test_edit_range_outside_fails_loudly(session, project):
    session.write("out.txt", "alpha\nbeta\nalpha\n")
    with pytest.raises(ValueError):
        session.edit("out.txt", "beta", "BETA", start_line=1, stop_line=1)
    # the failed edit staged nothing new
    assert session.staged()[project / "out.txt"] == "alpha\nbeta\nalpha\n"


def test_edit_rejects_invalid_line_ranges(session, project):
    session.write("r.txt", "one\ntwo\n")
    for kwargs in (
        {"start_line": 0},
        {"start_line": 2, "stop_line": 1},
        {"stop_line": 99},
        {"start_line": 99},
    ):
        with pytest.raises(ValueError, match="invalid line range"):
            session.edit("r.txt", "one", "ONE", **kwargs)


def test_edit_binary_raises(session, project):
    session.write("bin.dat", b"\x00\x01")
    with pytest.raises(ValueError, match="binary"):
        session.edit("bin.dat", "x", "y")


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


def test_glob_does_not_descend_into_symlinked_dirs(session, project):
    # the target sits outside the root, so the only way in is the
    # link; git never traverses symlinks for discovery either
    outside = project.parent / 'outside-tree'
    outside.mkdir()
    (outside / 'z.py').write_text('z = 1\n')
    (project / 'src' / 'shortcut').symlink_to(outside, target_is_directory=True)
    names = {p.name for p in session.glob('**/*.py')}
    assert 'z.py' not in names


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
