"""apply_diff_git: git-canonical patches, applied by libgit2.

Every kind git has a format for -- binary payloads, symlinks, modes --
lives here. The unidiff path reads text hunks only and says so.
"""

import pytest

from pyedit.diff import file_patch as _git_section
from pyedit.gitpatch import GitPatchError, apply_patch
from pyedit.session import EditSession, Symlink


def test_binary_patch_updates_a_file(project):
    (project / "x.bin").write_bytes(b"\x00\x01\x02\x03")
    session = EditSession()
    apply_patch(
        session, _git_section("x.bin", b"\x00\x01\x02\x03", b"\x00\x01\x02\x03\x04")
    )
    assert session.staged()[project / "x.bin"] == b"\x00\x01\x02\x03\x04"


def test_binary_patch_creates_a_file(project):
    session = EditSession()
    apply_patch(session, _git_section("fresh.bin", None, b"\x00\x01\x02"))
    assert session.staged()[project / "fresh.bin"] == b"\x00\x01\x02"


def test_binary_patch_deletes_a_file(project):
    (project / "x.bin").write_bytes(b"\x00\x01\x02")
    session = EditSession()
    apply_patch(session, _git_section("x.bin", b"\x00\x01\x02", None))
    assert session.staged()[project / "x.bin"] is None


def test_binary_patch_onto_different_content_raises(project):
    # libgit2 reverses the patch back onto the preimage to verify it,
    # so a payload cut from another file fails instead of corrupting
    (project / "x.bin").write_bytes(b"\xff\xfe\xfd\xfc")
    session = EditSession()
    with pytest.raises(GitPatchError):
        apply_patch(
            session,
            _git_section("x.bin", b"\x00\x01\x02\x03", b"\x00\x01\x02\x03\x04"),
        )
    assert session.staged() == {}


def test_binary_patch_without_a_git_header_raises(project):
    session = EditSession()
    with pytest.raises(GitPatchError):
        apply_patch(session, "GIT binary patch\nliteral 0\n\n")


def test_staged_text_wins_over_a_symlink_on_disk(project):
    # the path is a link out there, but the session already replaced
    # it with text: the patch must see the text
    (project / "link").symlink_to("src/a.py")
    session = EditSession()
    session.write("link", "now text\n")
    apply_patch(session, _git_section("link", "now text\n", b"\x00\x01"))
    assert session.staged()[project / "link"] == b"\x00\x01"


def test_symlink_patch_creates_a_link(project):
    session = EditSession()
    apply_patch(session, _git_section("link", None, Symlink("a.py")))
    assert session.staged()[project / "link"] == Symlink("a.py")


def test_symlink_patch_retargets_a_link(project):
    (project / "link").symlink_to("src/a.py")
    session = EditSession()
    apply_patch(session, _git_section("link", Symlink("src/a.py"), Symlink("src/b.py")))
    assert session.staged()[project / "link"] == Symlink("src/b.py")


def test_symlink_patch_deletes_a_link(project):
    (project / "link").symlink_to("src/a.py")
    session = EditSession()
    apply_patch(session, _git_section("link", Symlink("src/a.py"), None))
    assert session.staged()[project / "link"] is None


def test_symlink_patch_replaces_a_link_with_a_file(project):
    # git writes this as two sections: delete the 120000 entry, then
    # create a regular one
    (project / "link").symlink_to("src/a.py")
    session = EditSession()
    apply_patch(session, _git_section("link", Symlink("src/a.py"), "real file\n"))
    assert session.staged()[project / "link"] == "real file\n"


def test_a_text_patch_applies_too(project):
    session = EditSession()
    apply_patch(session, _git_section("src/a.py", "alpha = 1\n", "alpha = 42\n"))
    assert session.staged()[project / "src" / "a.py"] == "alpha = 42\nbeta = 2\n"
