from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from pyedit import Symlink
from pyedit.session import DiskChanged, EditSession

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def session(project: Path) -> EditSession:
    return EditSession()


def test_splice_applies_many_spans_in_one_pass(
    session: EditSession, project: Path
) -> None:
    session.write("s.py", "alpha = 1\nbeta = 2\ngamma = 3\n")
    n = session.splice(
        "s.py",
        [
            (1, 8, 1, 9, "42"),
            (3, 0, 3, 5, "delta"),
        ],
    )
    assert n == 2
    assert session.staged()[project / "s.py"] == ("alpha = 42\nbeta = 2\ndelta = 3\n")


def test_splice_converts_ast_byte_columns(session: EditSession, project: Path) -> None:
    text = 's = "\u03b1\u03b1\u03b1"\ntotal = \u03b1 + x\n'
    session.write("u.py", text)
    import ast

    tree = ast.parse(text)
    stmt = tree.body[1]
    assert isinstance(stmt, ast.Assign)
    assert isinstance(stmt.value, ast.BinOp)
    name = stmt.value.right
    assert name.col_offset != 0
    assert name.end_col_offset is not None
    n = session.splice("u.py", [(2, name.col_offset, 2, name.end_col_offset, "y")])
    assert n == 1
    assert (
        session.staged()[project / "u.py"]
        == 's = "\u03b1\u03b1\u03b1"\ntotal = \u03b1 + y\n'
    )


def test_splice_spans_across_lines_and_checks_overlap(
    session: EditSession, project: Path
) -> None:
    session.write("m.py", "aa = 1\nbb = 2\ncc = 3\n")
    n = session.splice("m.py", [(1, 0, 3, 0, "ab = 12\n")])
    assert n == 1
    assert session.staged()[project / "m.py"] == "ab = 12\ncc = 3\n"
    with pytest.raises(ValueError, match="overlap"):
        session.splice(
            "m.py",
            [(1, 0, 1, 4, "x"), (1, 2, 1, 6, "y")],
        )


def test_free_names_finds_uses_without_bindings(
    session: EditSession, project: Path
) -> None:
    session.write(
        "f.py",
        "import os\n\n\ndef main(x):\n    return os.sep + str(x) + SEP\n",
    )
    assert session.free_names("f.py") == {"SEP"}


def test_glob_matches_relative_paths(session: EditSession) -> None:
    assert [p.name for p in session.glob("src/*.py")] == ["a.py", "b.py"]


def test_glob_lists_a_directory_link_but_never_descends(
    session: EditSession, project: Path
) -> None:
    (project / "srcdir").symlink_to("src")
    matches = [p.name for p in session.glob("*")]
    assert "srcdir" in matches
    assert session.glob("srcdir/*.py") == []
    assert session.is_link("srcdir")
    assert not session.is_link("src/a.py")


def test_glob_sees_a_staged_link(session: EditSession, project: Path) -> None:
    session.symlink("src/a.py", "linked.py")
    assert [p.name for p in session.glob("linked.py")] == ["linked.py"]
    assert [p.name for p in session.glob("**/*.py")] == [
        "linked.py",
        "a.py",
        "b.py",
    ]
    assert session.glob("*.txt") == []


def test_glob_merges_staged_files(session: EditSession, project: Path) -> None:
    session.write("src/new.py", "x\n")
    session.delete("src/a.py")
    assert [p.name for p in session.glob("src/*.py")] == ["b.py", "new.py"]


def test_read_materializes_disk_content(session: EditSession, project: Path) -> None:
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_prune_drops_materialized_reads(session: EditSession, project: Path) -> None:
    session.read("src/a.py")
    session.prune_unchanged()
    assert session.staged() == {}


def test_read_cache_follows_disk_after_an_outside_write(
    session: EditSession, project: Path
) -> None:
    """A read is a cache, not an edit: a formatter rewriting the file
    after it shows through, and nothing stale stays to be applied."""
    session.read("src/a.py")
    (project / "src" / "a.py").write_text("alpha=1\n")
    session.prune_unchanged()
    assert session.staged() == {}
    assert session.read("src/a.py") == "alpha=1\n"
    (project / "src" / "a.py").write_text("alpha = 1\n")
    assert session.read("src/a.py") == "alpha = 1\n"
    session.apply()
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"


def test_apply_refuses_paths_changed_on_disk_after_staging(
    session: EditSession, project: Path
) -> None:
    """An outside write after staging would be lost; apply names it and
    writes nothing, not even the paths that did not move."""
    session.edit("src/a.py", "alpha = 1", "alpha = 2")
    session.write("src/b.py", "gamma = 4\n")
    (project / "src" / "a.py").write_text("alpha = 1\nbeta = 2\nC = 3\n")
    with pytest.raises(DiskChanged, match=r"src/a.py") as refused:
        session.apply()
    assert refused.value.paths == [project / "src" / "a.py"]
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\nC = 3\n"
    assert (project / "src" / "b.py").read_text() == "gamma = 3\n"


def test_apply_refuses_a_new_file_someone_else_created(
    session: EditSession, project: Path
) -> None:
    session.write("src/new.py", "mine\n")
    (project / "src" / "new.py").write_text("theirs\n")
    with pytest.raises(DiskChanged):
        session.apply()


def test_apply_accepts_disk_that_already_holds_the_staged_text(
    session: EditSession, project: Path
) -> None:
    session.edit("src/a.py", "alpha = 1", "alpha = 2")
    (project / "src" / "a.py").write_text("alpha = 2\nbeta = 2\n")
    session.apply()
    assert (project / "src" / "a.py").read_text() == "alpha = 2\nbeta = 2\n"


def test_rollback_restores_the_bases(session: EditSession, project: Path) -> None:
    mark = session.checkpoint()
    session.edit("src/a.py", "alpha = 1", "alpha = 2")
    session.rollback(mark)
    (project / "src" / "a.py").write_text("moved\n")
    session.write("src/a.py", "mine\n")
    session.apply()
    assert (project / "src" / "a.py").read_text() == "mine\n"


def test_read_sees_disk_content(session: EditSession, project: Path) -> None:
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"


def test_read_sees_staged_write(session: EditSession, project: Path) -> None:
    session.write("src/a.py", "rewritten\n")
    assert session.read("src/a.py") == "rewritten\n"


def test_write_accepts_str_and_bytes_only(session: EditSession, project: Path) -> None:
    session.write("src/a.py", b"bytes\n")
    assert session.read("src/a.py") == b"bytes\n"
    with pytest.raises(TypeError):
        # wrong type on purpose: write must raise TypeError
        session.write("src/a.py", 42)  # pyrefly: ignore[bad-argument-type]


def test_relative_paths_resolve_against_root(tmp_path: Path) -> None:
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


def test_edit_replaces_and_counts(session: EditSession, project: Path) -> None:
    n = session.edit("src/a.py", "alpha", "ALPHA")
    assert n == 1
    assert session.read("src/a.py") == "ALPHA = 1\nbeta = 2\n"


def test_edit_missing_pattern_raises(session: EditSession, project: Path) -> None:
    with pytest.raises(ValueError):
        session.edit("src/a.py", "missing", "x")
    # the read materialized the original content; the edit staged nothing
    assert session.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\n"


def test_edit_empty_pattern_raises(session: EditSession, project: Path) -> None:
    # "" counts len+1 times and replaces at every position: without the
    # guard it splices the replacement between every character
    with pytest.raises(ValueError):
        session.edit("src/a.py", "", "x")
    assert session.read("src/a.py") == "alpha = 1\nbeta = 2\n"


def test_edit_range_scopes_duplicates(session: EditSession, project: Path) -> None:
    session.write("dup.txt", "x = 1\nx = 1\nx = 1\n")
    n = session.edit("dup.txt", "x = 1", "x = 2", start_line=2, stop_line=2)
    assert n == 1
    assert session.read("dup.txt") == "x = 1\nx = 2\nx = 1\n"


def test_edit_range_is_inclusive_and_spans_lines(
    session: EditSession, project: Path
) -> None:
    session.write("multi.txt", "a\nb\nc\nd\n")
    n = session.edit("multi.txt", "b\nc", "B\nC", start_line=2, stop_line=3)
    assert n == 1
    assert session.read("multi.txt") == "a\nB\nC\nd\n"


def test_edit_match_crossing_boundary_needs_the_newline_in_range(
    session: EditSession, project: Path
) -> None:
    session.write("bound.txt", "ab\nab\n")
    with pytest.raises(ValueError):
        session.edit("bound.txt", "b\na", "X", start_line=1, stop_line=1)
    n = session.edit("bound.txt", "b\na", "X", start_line=1, stop_line=2)
    assert n == 1
    assert session.read("bound.txt") == "aXb\n"


def test_edit_range_outside_fails_loudly(session: EditSession, project: Path) -> None:
    session.write("out.txt", "alpha\nbeta\nalpha\n")
    with pytest.raises(ValueError):
        session.edit("out.txt", "beta", "BETA", start_line=1, stop_line=1)
    # the failed edit staged nothing new
    assert session.staged()[project / "out.txt"] == "alpha\nbeta\nalpha\n"


def test_edit_rejects_invalid_line_ranges(session: EditSession, project: Path) -> None:
    session.write("r.txt", "one\ntwo\n")
    for kwargs in (
        {"start_line": 0},
        {"start_line": 2, "stop_line": 1},
        {"stop_line": 99},
        {"start_line": 99},
    ):
        with pytest.raises(ValueError, match="invalid line range"):
            session.edit("r.txt", "one", "ONE", **kwargs)


def test_edit_re_replaces_with_backrefs_and_counts(
    session: EditSession, project: Path
) -> None:
    session.write("r.py", 'x = variables["key"]\ny = other["keep"]\n')
    n = session.edit_re("r.py", r'variables\["(\w+)"\]', r"args.\1")
    assert n == 1
    assert session.staged()[project / "r.py"] == 'x = args.key\ny = other["keep"]\n'


def test_edit_re_fails_on_zero_matches(session: EditSession, project: Path) -> None:
    session.write("r.py", "a = 1\n")
    with pytest.raises(ValueError, match="pattern not found"):
        session.edit_re("r.py", r"nothing", "x")


def test_edit_re_respects_count_and_range(session: EditSession, project: Path) -> None:
    session.write("r.py", "cmd 1\ncmd 2\ncmd 3\n")
    n = session.edit_re("r.py", r"cmd \d", "run", count=1)
    assert n == 1
    assert session.staged()[project / "r.py"] == "run\ncmd 2\ncmd 3\n"
    n = session.edit_re("r.py", r"cmd", "run", start_line=2, stop_line=3)
    assert n == 2
    assert session.staged()[project / "r.py"] == "run\nrun 2\nrun 3\n"


def test_find_returns_positions_that_feed_splice(
    session: EditSession, project: Path
) -> None:
    session.write("f.py", "alpha one\nbeta two\nalpha three\n")
    hits = session.find("f.py", r"alpha (\w+)")
    assert hits == [(1, 0, "alpha one"), (3, 0, "alpha three")]
    n = session.splice("f.py", [(ln, c, ln, c + len(t), "X") for ln, c, t in hits])
    assert n == 2
    assert session.staged()[project / "f.py"] == "X\nbeta two\nX\n"


def test_edit_binary_raises(session: EditSession, project: Path) -> None:
    session.write("bin.dat", b"\x00\x01")
    with pytest.raises(ValueError, match="binary"):
        session.edit("bin.dat", "x", "y")


def test_delete_and_rename(session: EditSession, project: Path) -> None:
    session.rename("src/a.py", "src/c.py")
    staged = session.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "c.py"] == "alpha = 1\nbeta = 2\n"


def test_delete_missing_raises(session: EditSession) -> None:
    with pytest.raises(FileNotFoundError):
        session.delete("src/nope.py")


def test_rename_missing_raises(session: EditSession) -> None:
    with pytest.raises(FileNotFoundError):
        session.rename("src/nope.py", "src/x.py")


def test_rename_moves_a_directory_tree(session: EditSession, project: Path) -> None:
    session.write("pkg/one.py", "a = 1\n")
    session.write("pkg/sub/two.py", "b = 2\n")
    session.rename("pkg", "renamed")
    staged = session.staged()
    assert staged[project / "pkg" / "one.py"] is None
    assert staged[project / "renamed" / "one.py"] == "a = 1\n"
    assert staged[project / "renamed" / "sub" / "two.py"] == "b = 2\n"


def test_directory_rename_keeps_staged_deletions_deleted(
    session: EditSession, project: Path
) -> None:
    session.write("pkg/one.py", "a = 1\n")
    session.write("pkg/gone.py", "b = 2\n")
    session.delete("pkg/gone.py")
    session.rename("pkg", "renamed")
    staged = session.staged()
    assert staged[project / "pkg" / "gone.py"] is None
    assert project / "renamed" / "gone.py" not in staged


def test_directory_rename_includes_files_created_this_run(
    session: EditSession, project: Path
) -> None:
    session.write("docs/extra.md", "x\n")
    session.rename("docs", "renamed")
    staged = session.staged()
    assert staged[project / "renamed" / "note.txt"] == "hello\n"
    assert staged[project / "renamed" / "extra.md"] == "x\n"


def test_directory_rename_onto_existing_path_raises(session: EditSession) -> None:
    with pytest.raises(FileExistsError):
        session.rename("docs", "src")


def test_directory_rename_into_itself_raises(session: EditSession) -> None:
    with pytest.raises(ValueError):
        session.rename("docs", "docs/inner")


def test_rename_preserves_a_symlink(session: EditSession, project: Path) -> None:
    (project / "target.txt").write_text("t\n")
    (project / "link.txt").symlink_to("target.txt")
    session.rename("link.txt", "moved.txt")
    staged = session.staged()
    assert staged[project / "link.txt"] is None
    assert staged[project / "moved.txt"] == Symlink("target.txt")
    session.apply()
    assert (project / "moved.txt").is_symlink()
    assert os.readlink(project / "moved.txt") == "target.txt"
    assert not (project / "link.txt").exists()


def test_created_file_is_not_executable(session: EditSession, project: Path) -> None:
    session.write("src/fresh.py", "alpha = 1\n")
    session.apply()
    assert not (project / "src" / "fresh.py").stat().st_mode & 0o111


def test_symlink_creates_a_link(session: EditSession, project: Path) -> None:
    session.symlink("AGENTS.md", "CLAUDE.md")
    assert session.staged()[project / "CLAUDE.md"] == Symlink("AGENTS.md")
    session.apply()
    assert (project / "CLAUDE.md").is_symlink()
    assert os.readlink(project / "CLAUDE.md") == "AGENTS.md"


def test_symlink_force_retargets_an_existing_link(
    session: EditSession, project: Path
) -> None:
    (project / "link.txt").symlink_to("target.txt")
    session.symlink("other.txt", "link.txt", force=True)
    assert session.staged()[project / "link.txt"] == Symlink("other.txt")


def test_symlink_onto_existing_path_raises(session: EditSession) -> None:
    session.write("docs/anchor.md", "x\n")
    with pytest.raises(FileExistsError):
        session.symlink("anchor.md", "docs/anchor.md")


def test_read_of_a_staged_symlink_refuses(session: EditSession) -> None:
    session.symlink("AGENTS.md", "CLAUDE.md")
    with pytest.raises(ValueError, match="symlink"):
        session.read("CLAUDE.md")


def test_directory_rename_moves_symlinks(session: EditSession, project: Path) -> None:
    (project / "docs" / "link.txt").symlink_to("note.txt")
    session.rename("docs", "renamed")
    assert session.staged()[project / "renamed" / "link.txt"] == Symlink("note.txt")
    session.apply()
    assert (project / "renamed" / "link.txt").is_symlink()


def test_glob_does_not_descend_into_symlinked_dirs(
    session: EditSession, project: Path
) -> None:
    # the target sits outside the root, so the only way in is the
    # link; git never traverses symlinks for discovery either
    outside = project.parent / "outside-tree"
    outside.mkdir()
    (outside / "z.py").write_text("z = 1\n")
    (project / "src" / "shortcut").symlink_to(outside, target_is_directory=True)
    names = {p.name for p in session.glob("**/*.py")}
    assert "z.py" not in names


def test_read_after_delete_raises(session: EditSession, project: Path) -> None:
    session.delete("src/a.py")
    with pytest.raises(FileNotFoundError):
        session.read("src/a.py")


def test_prune_unchanged_drops_identical_writes(
    session: EditSession, project: Path
) -> None:
    session.write("src/a.py", "alpha = 1\nbeta = 2\n")
    session.write("src/b.py", "changed\n")
    session.prune_unchanged()
    staged = session.staged()
    assert project / "src" / "a.py" not in staged
    assert staged[project / "src" / "b.py"] == "changed\n"


def test_new_file_staging(session: EditSession, project: Path) -> None:
    session.write("src/new.py", "fresh = 1\n")
    assert session.staged()[project / "src" / "new.py"] == "fresh = 1\n"


def test_describe_and_author_stage_meta(session: EditSession) -> None:
    session.describe("new subject")
    session.author("New Name <new@example.com>")
    assert session.meta == {
        "description": "new subject",
        "author": "New Name <new@example.com>",
    }


def test_author_rejects_anything_but_name_email(session: EditSession) -> None:
    for bad in ["noname", "Name <>", "<e@x>", "Name <a b>", "Name <no-close"]:
        with pytest.raises(ValueError, match="Name <email>"):
            session.author(bad)
    assert session.meta == {}


def test_finish_refuses_meta_without_a_revision(session: EditSession) -> None:
    from pyedit import runner

    session.describe("lost message")
    with pytest.raises(runner.MetaWithoutTarget, match="only -r REV"):
        runner.finish(session, runner.Options())
