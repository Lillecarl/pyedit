"""The git three-way merge prototype: what it must never do.

The rule these pin: a conflict is raised, never resolved quietly.
"""

import pytest

import pyedit
from pyedit import Collision, VFS
from pyedit.session import EditSession, Symlink


@pytest.fixture
def root(project, monkeypatch):
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    return session


def test_binary_divergence_raises_and_stages_nothing(root, project):
    """A conflicted index yields every stage, not just stage 0.

    Taking them in order stages the path three times and the last
    stage wins, which resolves a real conflict to 'theirs' in silence.
    """
    (project / "blob.bin").write_bytes(b"\x00old")
    with VFS():
        pyedit.write("blob.bin", b"\x00first")
    with pytest.raises(Collision):
        with VFS():
            pyedit.write("blob.bin", b"\x00second")
    assert root.staged_content(root.canon("blob.bin")) == b"\x00first"


def test_delete_against_edit_raises(root, project):
    with VFS():
        pyedit.delete("src/a.py")
    with pytest.raises(Collision):
        with VFS():
            pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")


def test_two_creates_with_different_content_raise(root, project):
    with VFS():
        pyedit.write("new.py", "one = 1\n")
    with pytest.raises(Collision):
        with VFS():
            pyedit.write("new.py", "two = 2\n")


def test_same_line_edited_twice_raises(root, project):
    with VFS():
        pyedit.edit("src/a.py", "beta = 2\n", "beta = 20\n")
    with pytest.raises(Collision):
        with VFS():
            pyedit.edit("src/a.py", "beta = 2\n", "beta = 99\n")


def test_neighbouring_lines_collide(root, project):
    """git merges two regions only with an unchanged line between them.

    One line of separation is the whole difference, so the message has
    to say what to do instead.
    """
    with VFS():
        pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
    with pytest.raises(Collision, match="make both edits in one scope"):
        with VFS():
            pyedit.edit("src/a.py", "beta = 2\n", "beta = 20\n")
    assert root.staged_content(root.canon("src/a.py")) == "alpha = 10\nbeta = 2\n"


def test_one_unchanged_line_is_enough(root, project):
    (project / "src" / "a.py").write_text("alpha = 1\nmiddle\nbeta = 2\n")
    with VFS():
        pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
    with VFS():
        pyedit.edit("src/a.py", "beta = 2\n", "beta = 20\n")
    assert root.staged_content(root.canon("src/a.py")) == (
        "alpha = 10\nmiddle\nbeta = 20\n"
    )


def test_symlink_retarget_merges_beside_a_text_edit(root, project):
    """A kind pyedit's own merge cannot do: it treats a link as bytes."""
    (project / "link").symlink_to("src/a.py")
    with VFS():
        pyedit.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
    with VFS():
        pyedit.symlink("src/b.py", "link", force=True)
    assert root.staged_content(root.canon("link")) == Symlink("src/b.py")
    assert root.staged_content(root.canon("src/a.py")) == "alpha = 10\nbeta = 2\n"


