from pyedit.diff import unified_diffs
from pyedit.session import EditSession


def staged(project, actions):
    session = EditSession()
    actions(session)
    return session.staged()


def test_modified_diff_headers(project):
    def edit(session):
        session.write(project / "src" / "a.py", "alpha = 2\nbeta = 2\n")

    results = unified_diffs(staged(project, edit))
    rel, diff = results[0]
    assert rel == "src/a.py"
    assert "--- a/src/a.py\n" in diff
    assert "+++ b/src/a.py\n" in diff
    assert "-alpha = 1\n" in diff
    assert "+alpha = 2\n" in diff
    assert "@@ -1,2 +1,2 @@\n" in diff


def test_new_file_diff_is_dev_null(project):
    def write_new(session):
        session.write(project / "src" / "c.py", "fresh = 1\n")

    results = unified_diffs(staged(project, write_new))
    rel, diff = results[0]
    assert rel == "src/c.py"
    assert "--- /dev/null\n" in diff
    assert "+++ b/src/c.py\n" in diff
    assert "+fresh = 1\n" in diff


def test_deleted_diff_to_dev_null(project):
    (project / "src" / "a.py").write_text("alpha = 1\n")

    def delete(session):
        session.delete(project / "src" / "a.py")

    results = unified_diffs(staged(project, delete))
    _, diff = results[0]
    assert "--- a/src/a.py\n" in diff
    assert "+++ /dev/null\n" in diff
    assert "-alpha = 1\n" in diff


def test_identical_write_yields_no_entry(project, tmp_path):
    (project / "src" / "a.py").write_text("alpha = 1\n")

    def noop(session):
        session.write(project / "src" / "a.py", "alpha = 1\n")

    assert unified_diffs(staged(project, noop)) == []


def test_context_lines_respected(project):
    (project / "src" / "a.py").write_text("1\n2\n3\n4\n5\n6\n7\n8\n9\n")

    def edit(session):
        session.write(project / "src" / "a.py", "1\n2\n3\n4\n5\nX\n7\n8\n9\n")

    _, diff = unified_diffs(staged(project, edit), context=2)[0]
    assert "@@ -4,5 +4,5 @@\n" in diff
