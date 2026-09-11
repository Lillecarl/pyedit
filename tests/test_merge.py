import pytest

from pyedit import Collision, VFS


def test_sibling_scopes_merge_into_the_collector(project):
    with VFS() as root:
        with VFS(root) as fs:
            fs.write("src/a.py", "alpha = 1\nbeta = 2\nALPHA = 11\n")
        with VFS(root) as fs:
            fs.write("src/b.py", "gamma = 3\ndelta = 4\n")
    staged = root.staged()
    assert set(staged) == {project / "src" / "a.py", project / "src" / "b.py"}
    assert staged[project / "src" / "b.py"] == "gamma = 3\ndelta = 4\n"


def test_second_scope_ignores_lines_moved_by_the_first(project):
    with VFS() as root:
        with VFS(root) as first:
            first.edit("src/a.py", "alpha = 1\n", "alpha = 1\n# inserted header\n# more\n# lines\n")
        with VFS(root) as second:
            second.edit("src/a.py", "beta = 2\n", "beta = 22\n")
    merged = root.staged_content(project / "src" / "a.py")
    assert merged == (
        "alpha = 1\n# inserted header\n# more\n# lines\nbeta = 22\n"
    )


def test_conflicting_edits_of_the_same_line_raise(project):
    with VFS() as root:
        with VFS(root) as first:
            first.edit("src/a.py", "beta = 2\n", "beta = 20\n")
        with pytest.raises(Collision, match="context not found"):
            with VFS(root) as second:
                second.edit("src/a.py", "beta = 2\n", "beta = 99\n")
    assert root.staged_content(project / "src" / "a.py") == "alpha = 1\nbeta = 20\n"


def test_failed_merge_keeps_earlier_merges(project):
    with VFS() as root:
        with VFS(root) as first:
            first.write("src/b.py", "gamma = 3\n_epsilon = 5\n")
        with pytest.raises(ValueError, match="pattern not found"):
            with VFS(root) as second:
                second.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
                second.edit("src/a.py", "beta = 999\n", "beta = 1000\n")
    assert root.staged_content(project / "src" / "b.py") == "gamma = 3\n_epsilon = 5\n"
    assert (project / "src" / "a.py") not in root.staged()


def test_delete_and_edit_conflict(project):
    with VFS() as root:
        with VFS(root) as first:
            first.delete("src/a.py")
        with pytest.raises(Collision, match="deleted by an earlier edit"):
            with VFS(root) as second:
                second.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")


def test_edit_after_delete_conflict(project):
    with VFS() as root:
        with VFS(root) as first:
            first.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")
        with pytest.raises(Collision, match="changed by an earlier edit"):
            with VFS(root) as second:
                second.delete("src/a.py")


def test_double_create_with_different_content(project):
    with VFS() as root:
        with VFS(root) as first:
            first.write("new.py", "one = 1\n")
        with pytest.raises(Collision, match="created by two edits"):
            with VFS(root) as second:
                second.write("new.py", "two = 2\n")


def test_double_create_with_same_content_is_idempotent(project):
    with VFS() as root:
        with VFS(root) as first:
            first.write("new.py", "one = 1\n")
        with VFS(root) as second:
            second.write("new.py", "one = 1\n")
    assert root.staged_content(project / "new.py") == "one = 1\n"


def test_ambiguous_context_raises(project):
    block = "".join(f"line{d}\n" for d in range(1, 13))
    content = block + block
    changed = content.replace("line4\n", "LINE4\n", 1) + "appended\n"
    with VFS() as root:
        with VFS(root) as first:
            first.write("src/dup.py", content + "appended\n")
        with pytest.raises(Collision, match="ambiguous"):
            with VFS(root) as second:
                second.write("src/dup.py", changed)


def test_unique_context_despite_repetition_applies(project):
    block = "".join(f"line{d}\n" for d in range(1, 13))
    content = block + block
    (project / "src" / "dup.py").write_text(content)
    with VFS() as root:
        with VFS(root) as first:
            first.edit("src/dup.py", "line1\n", "LINE1\n", count=1)
        with VFS(root) as second:
            second.edit("src/dup.py", "line12\n", "line12\nextra\n", count=1)
    merged = root.staged_content(project / "src" / "dup.py")
    assert merged == content.replace("line12\n", "line12\nextra\n", 1).replace(
        "line1\n", "LINE1\n", 1
    )


def test_failed_collector_discards_merges(project):
    with pytest.raises(RuntimeError):
        with VFS() as root:
            with VFS(root) as fs:
                fs.write("src/a.py", "alpha = 10\nbeta = 2\n")
            raise RuntimeError("agent changed its mind")
    assert root.staged() == {}


def test_reading_scope_pollutes_nothing(project):
    with VFS() as root:
        with VFS(root) as fs:
            fs.read("src/a.py")
            fs.glob("**/*.py")
    assert root.staged() == {}


def test_binary_conflict(project):
    (project / "blob.bin").write_bytes(b"\x00old")
    with VFS() as root:
        with VFS(root) as first:
            first.write("blob.bin", b"\x00first")
        with pytest.raises(Collision, match="binary"):
            with VFS(root) as second:
                second.write("blob.bin", b"\x00second")


def test_rename_as_delete_plus_create(project):
    with VFS() as root:
        with VFS(root) as first:
            first.rename("src/a.py", "src/renamed.py")
        with pytest.raises(Collision, match="deleted by an earlier edit"):
            with VFS(root) as second:
                second.edit("src/a.py", "alpha = 1\n", "alpha = 10\n")


def test_apply_writes_merged_state(project):
    with VFS() as root:
        with VFS(root) as first:
            first.write("src/a.py", "alpha = 1\nbeta = 2\nnew = 3\n")
        with VFS(root) as fs:
            fs.write("brand.py", "fresh = True\n")
        root.apply()
    assert (project / "src" / "a.py").read_text() == (
        "alpha = 1\nbeta = 2\nnew = 3\n"
    )
    assert (project / "brand.py").read_text() == "fresh = True\n"


def test_nested_scopes(project):
    with VFS() as root:
        with VFS(root) as outer:
            with VFS(outer) as inner:
                inner.write("src/a.py", "alpha = 1\nbeta = 2\ncomment = 'added'\n")
            outer.edit("src/b.py", "gamma = 3\n", "gamma = 33\n")
    staged = root.staged()
    assert staged[project / "src" / "a.py"] == (
        "alpha = 1\nbeta = 2\ncomment = 'added'\n"
    )
    assert staged[project / "src" / "b.py"] == "gamma = 33\n"


def test_collector_is_reusable_after_a_collision(project):
    with VFS() as root:
        with VFS(root) as first:
            first.edit("src/a.py", "beta = 2\n", "beta = 20\n")
        with pytest.raises(Collision):
            with VFS(root) as second:
                second.edit("src/a.py", "beta = 2\n", "beta = 99\n")
        with VFS(root) as third:
            third.edit("src/b.py", "gamma = 3\n", "gamma = 30\n")
    assert root.staged_content(project / "src" / "b.py") == "gamma = 30\n"


def test_non_vfs_parent_rejected():
    with pytest.raises(TypeError):
        VFS("not a vfs")
