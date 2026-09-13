import pytest

from pyedit.session import EditSession
from pyedit.udiff import UnifiedDiffError, apply_diff


GIT_DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,2 @@
-alpha = 1
+alpha = 42
 beta = 2
"""


def test_update_single_hunk(project):
    session = EditSession()
    applied, _failures = apply_diff(session, GIT_DIFF)
    assert applied[0].path == "src/a.py"
    assert applied[0].action == "updated"
    assert session.staged()[project / "src" / "a.py"] == "alpha = 42\nbeta = 2\n"


def test_update_multiple_hunks(project):
    (project / "src" / "a.py").write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        " l1\n"
        "-l2\n"
        "+L2\n"
        "@@ -8 +8 @@\n"
        "-l8\n"
        "+L8\n",
    )
    assert session.staged()[project / "src" / "a.py"] == (
        "l1\nL2\nl3\nl4\nl5\nl6\nl7\nL8\n"
    )


def test_create_and_delete_files(project):
    session = EditSession()
    apply_diff(
        session,
        "--- /dev/null\n"
        "+++ b/src/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n"
        "+two\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "deleted file mode 100644\n"
        "--- a/src/b.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gamma = 3\n",
    )
    staged = session.staged()
    assert staged[project / "src" / "new.txt"] == "one\ntwo\n"
    assert staged[project / "src" / "b.py"] is None


def test_no_prefix_paths(project):
    session = EditSession()
    apply_diff(
        session,
        "--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n-alpha = 1\n+alpha = 9\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 9\nbeta = 2\n"


def test_no_newline_markers_on_both_sides(project):
    (project / "src" / "a.py").write_text("alpha = 1")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-alpha = 1\n\\ No newline at end of file\n+alpha = 2\n\\ No newline at end of file\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 2"


def test_marker_only_on_old_side_adds_newline(project):
    (project / "src" / "a.py").write_text("alpha = 1")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-alpha = 1\n\\ No newline at end of file\n+alpha = 2\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 2\n"


def test_insertion_after_no_newline_file(project):
    (project / "src" / "a.py").write_text("alpha = 1")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,0 +2 @@\n+beta = 2\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_apply_matches_context_despite_trailing_whitespace(project):
    (project / "src" / "a.py").write_text("alpha = 1   \nbeta = 2\n")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n-alpha = 1\n+alpha = 42\n beta = 2\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 42\nbeta = 2\n"


def test_context_line_keeps_the_file_whitespace(project):
    (project / "src" / "a.py").write_text("alpha = 1\nbeta = 2\n")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n alpha = 1  \n-beta = 2\n+beta = 42\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 42\n"


def test_anchor_found_when_the_hint_is_past_the_end(project):
    (project / "src" / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -5,3 +5,3 @@\n two\n-three\n+THREE\n four\n",
    )
    assert (
        session.staged()[project / "src" / "a.py"]
        == "one\ntwo\nTHREE\nfour\nfive\n"
    )


def test_anchor_found_at_the_far_end_from_a_zero_hint(project):
    (project / "src" / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    session = EditSession()
    apply_diff(
        session,
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n three\n-four\n+FOUR\n five\n",
    )
    assert (
        session.staged()[project / "src" / "a.py"]
        == "one\ntwo\nthree\nFOUR\nfive\n"
    )


def test_context_mismatch_raises(project):
    session = EditSession()
    with pytest.raises(UnifiedDiffError, match="context not found"):
        apply_diff(
            session,
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-nothing here\n+x\n",
        )


def test_binary_source_raises(project):
    (project / "src" / "data.bin").write_bytes(b"\xff\xfe\x00\x01")
    session = EditSession()
    with pytest.raises(UnifiedDiffError, match="binary"):
        apply_diff(
            session,
            "--- a/src/data.bin\n+++ b/src/data.bin\n@@ -1 +1 @@\n-x\n+y\n",
        )


def test_rename_without_hunks(project):
    session = EditSession()
    applied, _failures = apply_diff(
        session,
        "diff --git a/src/a.py b/src/renamed.py\n"
        "similarity index 100%\n"
        "rename from src/a.py\n"
        "rename to src/renamed.py\n",
    )
    staged = session.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "renamed.py"] == "alpha = 1\nbeta = 2\n"
    assert applied[0].action == "renamed"


def test_multiple_files_in_one_diff(project):
    session = EditSession()
    apply_diff(
        session,
        GIT_DIFF
        + "diff --git a/docs/note.txt b/docs/note.txt\n"
        + "--- a/docs/note.txt\n"
        + "+++ b/docs/note.txt\n"
        + "@@ -1 +1 @@\n"
        + "-hello\n"
        + "+goodbye\n",
    )
    staged = session.staged()
    assert staged[project / "docs" / "note.txt"] == "goodbye\n"
    assert staged[project / "src" / "a.py"] == "alpha = 42\nbeta = 2\n"


def test_roundtrip_through_pyedit_output(project):
    session = EditSession()
    session.edit("src/a.py", "alpha", "ALPHA")
    from pyedit.diff import unified_diffs
    from pyedit.session import display_path

    produced = "".join(
        diff for _, diff in unified_diffs(session.staged())
    )
    roundtrip = EditSession()
    apply_diff(roundtrip, produced)
    assert roundtrip.staged()[project / "src" / "a.py"] == "ALPHA = 1\nbeta = 2\n"
