from pyedit import cli
from pyedit.diff import unified_diffs
from pyedit.udiff import apply_diff
from pyedit.session import EditSession


def staged(project, actions):
    session = EditSession()
    actions(session)
    return session.staged()


def test_symlink_changes_render_as_notes(project):
    session = EditSession()
    session.symlink("AGENTS.md", "CLAUDE.md")
    rel, text = unified_diffs(session.staged())[0]
    assert rel == "CLAUDE.md"
    assert "Symlink" in text
    assert "AGENTS.md" in text


def test_restaging_identical_bytes_is_a_no_op(project):
    (project / "img.bin").write_bytes(b"raw bytes")
    session = EditSession()
    session.write("img.bin", b"raw bytes")
    assert not session.diff().strip()
    session.prune_unchanged()
    assert project / "img.bin" not in session.staged()


def test_pure_rename_renders_rename_headers(project):
    session = EditSession()
    session.rename("src/a.py", "lib/a.py")
    [(rel, text)] = unified_diffs(session.staged())
    assert rel == "src/a.py"
    assert "similarity index 100%\n" in text
    assert "rename from src/a.py\n" in text
    assert "rename to lib/a.py\n" in text
    assert "alpha" not in text


def test_rename_diff_round_trips_through_the_parser(project):
    session = EditSession()
    session.rename("src/a.py", "lib/a.py")
    text = unified_diffs(session.staged())[0][1]
    moved = EditSession()
    applied, _failures = apply_diff(moved, text)
    staged = moved.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "lib" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_moved_and_edited_file_renders_delete_and_create(project):
    session = EditSession()
    session.rename("src/a.py", "lib/a.py")
    session.write("lib/a.py", "totally different\n")
    texts = [t for _, t in unified_diffs(session.staged())]
    assert not any("rename from" in t for t in texts)
    assert any(t.startswith("--- /dev/null") for t in texts)


def test_rename_diff_applies_with_git_apply(project):
    import subprocess

    session = EditSession()
    session.rename("src/a.py", "lib/a.py")
    diff_text = unified_diffs(session.staged())[0][1]
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "apply"],
        input=diff_text,
        text=True,
        cwd=project,
        check=True,
        capture_output=True,
    )
    assert (project / "lib" / "a.py").exists()
    assert not (project / "src" / "a.py").exists()


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


def test_identical_binary_stage_renders_nothing(project):
    # a no-op binary restage is not a change; the NUL sniff keeps
    # the disk side bytes so the comparison can see they are equal
    blob = b"\x00\x01\x02\n"
    (project / "data.bin").write_bytes(blob)
    session = EditSession()
    assert isinstance(session.read("data.bin"), bytes)
    session.write("data.bin", blob)
    assert session.diff() == ""
    session.write("data.bin", b"")
    assert "changed (4 -> 0 bytes)" in session.diff()
    session.write("data.bin", b"\xff")
    assert "changed (4 -> 1 bytes)" in session.diff()


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


def test_eof_newline_marker_on_added_file(project):
    def write(session):
        session.write(project / "fresh.txt", "first")

    _, diff = unified_diffs(staged(project, write))[0]
    assert "+first\n\\ No newline at end of file\n" in diff
    # every line of the diff is newline-terminated: no header glue
    assert all(line.endswith("\n") for line in diff.splitlines(keepends=True))


def test_eof_newline_marker_on_both_sides_changed(project):
    (project / "src" / "a.py").write_text("x=1")

    def edit(session):
        session.write(project / "src" / "a.py", "x=2")

    _, diff = unified_diffs(staged(project, edit))[0]
    assert "-x=1\n\\ No newline at end of file\n" in diff
    assert "+x=2\n\\ No newline at end of file\n" in diff


def test_eof_newline_marker_when_new_side_gains_newline(project):
    (project / "src" / "a.py").write_text("x=1")

    def edit(session):
        session.write(project / "src" / "a.py", "x=1\n")

    _, diff = unified_diffs(staged(project, edit))[0]
    assert "-x=1\n\\ No newline at end of file\n" in diff
    assert "+x=1\n" in diff
    assert diff.count("No newline") == 1


def test_eof_newline_marker_when_new_side_loses_newline(project):
    (project / "src" / "a.py").write_text("x=1\n")

    def edit(session):
        session.write(project / "src" / "a.py", "x=1")

    _, diff = unified_diffs(staged(project, edit))[0]
    assert "-x=1\n" in diff
    assert "+x=1\n\\ No newline at end of file\n" in diff
    assert diff.count("No newline") == 1


def test_eof_state_change_far_from_edit_gets_its_own_hunk(project):
    content = "a\nb\nc\nd\ne\nf\ng\nh\ni\nend\n"
    (project / "src" / "a.py").write_text(content)

    def edit(session):
        session.write(project / "src" / "a.py", content.replace("c\n", "X\n", 1)[:-1])

    _, diff = unified_diffs(staged(project, edit))[0]
    assert "-end\n+end\n\\ No newline at end of file\n" in diff


def test_multi_file_patch_stays_separated(project):
    def edits(session):
        session.write(project / "fresh.txt", "first")
        session.write(project / "src" / "b.py", "gamma = 3\nmore = 4\n")

    diffs = dict(unified_diffs(staged(project, edits)))
    fresh = diffs["fresh.txt"]
    assert fresh.endswith("\\ No newline at end of file\n")
    assert "--- a/src/b.py" not in fresh


def test_cli_patch_without_trailing_newline_renders_git_apply_clean_diff(
    project, capsys
):
    envelope = "*** Begin Patch\n*** Add File: fresh.txt\n+first\n*** End Patch\n"
    (project / "nl.envelope").write_text(envelope)
    target = project / "nl.diff"
    assert cli.main(["--patch", "nl.envelope", "-o", str(target)]) == 0
    rendered = target.read_text()
    assert rendered.endswith("+first\n\\ No newline at end of file\n")
