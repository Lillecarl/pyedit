"""diff_difflib: the same diff, hunks written by the standard library.

What it must keep from the libgit2 renderer, and the one thing that
differs.
"""

import pyedit
from pyedit.diff import difflib_hunks
from pyedit.render import DiffRenderer
from pyedit.session import EditSession, Symlink


def test_a_text_change_renders(project, monkeypatch):
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    pyedit.edit("src/a.py", "alpha", "ALPHA")
    text = pyedit.diff_difflib()
    assert "--- a/src/a.py" in text
    assert "+++ b/src/a.py" in text
    assert "-alpha = 1" in text
    assert "+ALPHA = 1" in text


def test_bytes_and_links_still_render_as_notes(project, monkeypatch):
    """Only libgit2 writes payloads and 120000 modes, so the notes are
    the same whichever renderer draws the hunks."""
    (project / "d.bin").write_bytes(b"\x00\x01")
    (project / "link").symlink_to("src/a.py")
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    pyedit.write("d.bin", b"\x00\x02")
    pyedit.symlink("src/b.py", "link", force=True)
    text = pyedit.diff_difflib()
    assert "Binary file d.bin" in text
    assert "Symlink link" in text


def _without_section_heading(hunks: str) -> str:
    """Drop what git writes after the `@@` pair; difflib writes none."""
    return "".join(
        line.split(" @@ ")[0] + " @@\n" if line.startswith("@@") else line
        for line in hunks.splitlines(keepends=True)
    )


def test_the_hunks_match_libgit2(project):
    """Bodies and boundaries agree; only the `@@` heading differs."""
    old = "".join(f"l{i}\n" for i in range(12))
    new = old.replace("l5\n", "L5\n")
    git = DiffRenderer().hunks(old, new, 3)
    assert "@@ -3,7 +3,7 @@ l1" in git  # git names the context
    assert _without_section_heading(git) == difflib_hunks(old, new, 3)


def test_git_names_the_enclosing_context_and_difflib_does_not(project):
    """The one measured difference between the two renderers."""
    old = "".join(f"l{i}\n" for i in range(20))
    new = "".join(("X\n" if i in (1, 18) else f"l{i}\n") for i in range(20))
    assert "@@ -16,5 +16,5 @@ l14" in DiffRenderer().hunks(old, new, 3)
    assert "@@ -16,5 +16,5 @@\n" in difflib_hunks(old, new, 3)


def test_no_newline_at_end_of_file_is_marked(project):
    assert difflib_hunks("a\n", "a\nb", 3).endswith(
        "+b\n\\ No newline at end of file\n"
    )


def test_identical_text_renders_nothing(project):
    assert difflib_hunks("a\n", "a\n", 3) == ""
