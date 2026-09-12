import pytest

import pyedit
from pyedit import Collision, VFS
from pyedit.session import EditSession


@pytest.fixture
def root(project, monkeypatch):
    """The root session, bound as the module's live session."""
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    return session


def test_sibling_scopes_merge_into_the_root(root, project):
    with VFS():
        pyedit.write("src/a.py", "alpha = 1\nbeta = 2\nALPHA = 11\n")
    with VFS():
        pyedit.write("src/b.py", "gamma = 3\ndelta = 4\n")
    staged = root.staged()
    assert set(staged) == {project / "src" / "a.py", project / "src" / "b.py"}
    assert staged[project / "src" / "b.py"] == "gamma = 3\ndelta = 4\n"


def test_scope_edits_route_through_pyedit_without_a_name(root):
    with VFS():
        pyedit.edit("src/a.py", "alpha", "ALPHA")
    assert root.staged_content(root.canon("src/a.py")) == "ALPHA = 1\nbeta = 2\n"


def test_root_edits_do_not_route_through_scopes(root):
    pyedit.edit("src/a.py", "alpha", "ALPHA")
    with VFS():
        assert pyedit.read("src/a.py") == "alpha = 1\nbeta = 2\n"  # disk truth
        pyedit.edit("src/a.py", "beta", "BETA")
    # the scope merged on top of the root's edit; its fresh-disk read
    # did not clobber it
    assert root.staged_content(root.canon("src/a.py")) == "ALPHA = 1\nBETA = 2\n"


def test_second_scope_ignores_lines_moved_by_the_first(root, project):
    with VFS():
        pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 1\n# inserted header\n# more\n# lines\n")
    with VFS():
        pyedit.edit("src/a.py", "beta = 2\n", "beta = 22\n")
    merged = root.staged_content(project / "src" / "a.py")
    assert merged == "alpha = 1\n# inserted header\n# more\n# lines\nbeta = 22\n"


def test_conflicting_edits_of_the_same_line_raise(root):
    with VFS():
        pyedit.edit("src/a.py", "beta = 2\n", "beta = 20\n")
    with pytest.raises(Collision, match="context not found"):
        with VFS():
            pyedit.edit("src/a.py", "beta = 2\n", "beta = 99\n")
    assert root.staged_content(root.canon("src/a.py")) == "alpha = 1\nbeta = 20\n"


def test_failed_scope_keeps_earlier_merges(root):
    with VFS():
        pyedit.write("src/b.py", "gamma = 3\n_epsilon = 5\n")
    with pytest.raises(ValueError, match="pattern not found"):
        with VFS():
            pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
            pyedit.edit("src/a.py", "beta = 999\n", "beta = 1000\n")
    assert root.staged_content(root.canon("src/b.py")) == "gamma = 3\n_epsilon = 5\n"
    assert root.canon("src/a.py") not in root.staged()


def test_delete_and_edit_conflict(root):
    with VFS():
        pyedit.delete("src/a.py")
    with pytest.raises(Collision, match="deleted by an earlier edit"):
        with VFS():
            pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")


def test_edit_after_delete_conflict(root):
    with VFS():
        pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
    with pytest.raises(Collision, match="changed by an earlier edit"):
        with VFS():
            pyedit.delete("src/a.py")


def test_double_create_with_different_content(root):
    with VFS():
        pyedit.write("new.py", "one = 1\n")
    with pytest.raises(Collision, match="created by two edits"):
        with VFS():
            pyedit.write("new.py", "two = 2\n")


def test_double_create_with_same_content_is_idempotent(root):
    with VFS():
        pyedit.write("new.py", "one = 1\n")
    with VFS():
        pyedit.write("new.py", "one = 1\n")
    assert root.staged_content(root.canon("new.py")) == "one = 1\n"


def test_ambiguous_context_raises(root):
    block = "".join(f"line{d}\n" for d in range(1, 13))
    content = block + block
    changed = content.replace("line4\n", "LINE4\n", 1) + "appended\n"
    with VFS():
        pyedit.write("src/dup.py", content + "appended\n")
    with pytest.raises(Collision, match="ambiguous"):
        with VFS():
            pyedit.write("src/dup.py", changed)


def test_unique_context_despite_repetition_applies(root, project):
    block = "".join(f"line{d}\n" for d in range(1, 13))
    content = block + block
    (project / "src" / "dup.py").write_text(content)
    with VFS():
        pyedit.edit("src/dup.py", "line1\n", "LINE1\n", count=1)
    with VFS():
        pyedit.edit("src/dup.py", "line12\n", "line12\nextra\n", count=1)
    merged = root.staged_content(project / "src" / "dup.py")
    assert merged == content.replace("line12\n", "line12\nextra\n", 1).replace(
        "line1\n", "LINE1\n", 1
    )


def test_failed_scope_discards_itself(root):
    with pytest.raises(RuntimeError):
        with VFS():
            pyedit.write("src/a.py", "alpha = 10\nbeta = 2\n")
            raise RuntimeError("agent changed its mind")
    assert root.staged() == {}


def test_reading_scope_pollutes_nothing(root):
    with VFS():
        pyedit.read("src/a.py")
        pyedit.glob("**/*.py")
    assert root.staged() == {}


def test_binary_conflict(root):
    (root.canon("blob.bin").parent / "blob.bin").write_bytes(b"\x00old")
    with VFS():
        pyedit.write("blob.bin", b"\x00first")
    with pytest.raises(Collision, match="binary"):
        with VFS():
            pyedit.write("blob.bin", b"\x00second")


def test_rename_as_delete_plus_create(root):
    with VFS():
        pyedit.rename("src/a.py", "src/renamed.py")
    with pytest.raises(Collision, match="deleted by an earlier edit"):
        with VFS():
            pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")


def test_apply_writes_merged_state(root, project):
    with VFS():
        pyedit.write("src/a.py", "alpha = 1\nbeta = 2\nnew = 3\n")
    with VFS():
        pyedit.write("brand.py", "fresh = True\n")
    root.apply()
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\nnew = 3\n"
    assert (project / "brand.py").read_text() == "fresh = True\n"


def test_diff_renders_the_merged_state(root):
    with VFS():
        pyedit.edit("src/a.py", "alpha", "ALPHA")
    diff = root.diff()
    assert "--- a/src/a.py" in diff
    assert "+ALPHA = 1" in diff


def test_nested_scopes(root, project):
    with VFS():
        with VFS():
            pyedit.write("src/a.py", "alpha = 1\nbeta = 2\ncomment = 'added'\n")
        pyedit.edit("src/b.py", "gamma = 3\n", "gamma = 33\n")
    staged = root.staged()
    assert staged[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\ncomment = 'added'\n"
    assert staged[project / "src" / "b.py"] == "gamma = 33\n"


def test_scopes_continue_after_a_collision(root):
    with VFS():
        pyedit.edit("src/a.py", "beta = 2\n", "beta = 20\n")
    with pytest.raises(Collision):
        with VFS():
            pyedit.edit("src/a.py", "beta = 2\n", "beta = 99\n")
    with VFS():
        pyedit.edit("src/b.py", "gamma = 3\n", "gamma = 30\n")
    assert root.staged_content(root.canon("src/b.py")) == "gamma = 30\n"
