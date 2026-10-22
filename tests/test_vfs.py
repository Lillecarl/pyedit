from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pyedit import vfs
from pyedit.diff import unified_diffs
from pyedit.session import EditSession

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture
def session(project: Path) -> EditSession:
    return EditSession()


@pytest.fixture
def patched(session: EditSession, project: Path) -> Iterator[EditSession]:
    restore = vfs.install(session)
    yield session
    restore()


def run_vfs(session: EditSession, code: str) -> dict[Path, str | bytes | None]:
    restore = vfs.install(session)
    try:
        exec(code, {"pyedit": session, "__name__": "__main__"})
    finally:
        restore()
    return session.staged()


# --- open() ---


def test_open_write_is_staged(
    patched: EditSession, project: Path, disk_text: Callable[[Path], str]
) -> None:
    with open(project / "src" / "a.py", "w") as f:
        f.write("rewritten\n")
    assert disk_text(project / "src" / "a.py") == "alpha = 1\nbeta = 2\n"
    assert patched.staged()[project / "src" / "a.py"] == "rewritten\n"


def test_open_append_seeds_with_current(patched: EditSession, project: Path) -> None:
    with open("src/a.py", "a") as f:
        f.write("more\n")
    assert patched.staged()[project / "src" / "a.py"] == "alpha = 1\nbeta = 2\nmore\n"


def test_open_x_fails_on_existing(patched: EditSession, project: Path) -> None:
    with pytest.raises(FileExistsError):
        # the open must raise; no handle is kept
        open("src/a.py", "x")  # noqa: SIM115


def test_open_x_creates_new(patched: EditSession, project: Path) -> None:
    with open("src/created.py", "x") as f:
        f.write("new\n")
    assert patched.staged()[project / "src" / "created.py"] == "new\n"


def test_open_rplus_edits(patched: EditSession, project: Path) -> None:
    with open("src/a.py", "r+") as f:
        f.write("ALPHA")
    assert patched.staged()[project / "src" / "a.py"] == "ALPHA = 1\nbeta = 2\n"


def test_open_read_sees_staged(patched: EditSession, project: Path) -> None:
    (project / "src" / "a.py").write_text("staged already\n")
    patched.write("src/a.py", "from overlay\n")
    with open("src/a.py") as f:
        assert f.read() == "from overlay\n"


def test_open_read_passes_through_unstaged(patched: EditSession, project: Path) -> None:
    with open("src/b.py") as f:
        assert f.read() == "gamma = 3\n"


def test_open_read_of_deleted_raises(patched: EditSession, project: Path) -> None:
    patched.delete("src/a.py")
    with pytest.raises(FileNotFoundError):
        # the open must raise; no handle is kept
        open("src/a.py")  # noqa: SIM115


def test_open_read_only_write_rejected(patched: EditSession, project: Path) -> None:
    with open("src/a.py") as f, pytest.raises(io.UnsupportedOperation):
        f.write("x")


def test_open_wplus_truncates(patched: EditSession, project: Path) -> None:
    with open("src/a.py", "w+") as f:
        f.write("only this\n")
        f.seek(0)
        assert f.read() == "only this\n"
    assert patched.staged()[project / "src" / "a.py"] == "only this\n"


def test_text_handle_on_binary_refused_at_open(
    patched: EditSession, project: Path
) -> None:
    # the real FS defers the decode failure to read time; the
    # overlay refuses at open, where the caller can still act
    (project / "blob.bin").write_bytes(b"\x00\x81\xfe")
    with pytest.raises(ValueError, match="binary"):
        # the open must raise; no handle is kept
        open(project / "blob.bin", "a")  # noqa: SIM115
    with pytest.raises(ValueError, match="binary"):
        # the open must raise; no handle is kept
        open(project / "blob.bin")  # noqa: SIM115


def test_open_binary_roundtrip(patched: EditSession, project: Path) -> None:
    with open("src/a.bin", "wb") as f:
        f.write(b"\x00\x01")
    with open("src/a.bin", "rb") as f:
        assert f.read() == b"\x00\x01"
    assert patched.staged()[project / "src" / "a.bin"] == b"\x00\x01"


def test_unclosed_file_never_stages(patched: EditSession, project: Path) -> None:
    # deliberately unclosed: nothing stages until close()
    f = open("src/a.py", "w")  # noqa: SIM115
    f.write("never closed\n")
    assert patched.staged() == {}
    f.close()
    assert patched.staged()[project / "src" / "a.py"] == "never closed\n"


# --- pathlib ---


def test_path_write_text_and_read_back(patched: EditSession, project: Path) -> None:
    p = Path("src/a.py")
    p.write_text("new text\n")
    assert p.read_text() == "new text\n"
    assert patched.staged()[project / "src" / "a.py"] == "new text\n"


def test_path_write_bytes_and_exists(patched: EditSession, project: Path) -> None:
    p = Path("src/data.bin")
    p.write_bytes(b"\x00\xff")
    assert p.exists()
    assert p.is_file()
    assert p.read_bytes() == b"\x00\xff"


def test_path_exists_and_is_file_overlay_aware(
    patched: EditSession, project: Path
) -> None:
    assert not Path("src/new.py").exists()
    Path("src/new.py").write_text("x\n")
    assert Path("src/new.py").exists()
    assert Path("src/new.py").is_file()
    Path("src/a.py").unlink()
    assert not Path("src/a.py").exists()
    assert Path("src").is_dir()


def test_path_unlink_missing_raises_and_missing_ok(patched: EditSession) -> None:
    with pytest.raises(FileNotFoundError):
        Path("src/nope.py").unlink()
    Path("src/nope.py").unlink(missing_ok=True)


def test_path_iterdir_merges_staged(patched: EditSession, project: Path) -> None:
    Path("src/new.py").write_text("x\n")
    Path("src/a.py").unlink()
    names = {p.name for p in Path("src").iterdir()}
    assert names == {"new.py", "b.py"}


def test_path_glob_merges_staged(patched: EditSession, project: Path) -> None:
    Path("src/new.py").write_text("x\n")
    Path("src/a.py").unlink()
    names = {p.name for p in Path("src").glob("*.py")}
    assert names == {"new.py", "b.py"}


def test_path_glob_star_stays_in_segment(patched: EditSession, project: Path) -> None:
    Path("src/sub/deep.py").write_text("x\n")
    assert {p.name for p in Path("src").glob("*.py")} == {"a.py", "b.py"}
    assert {p.name for p in Path("src").glob("**/*.py")} == {
        "a.py",
        "b.py",
        "deep.py",
    }


def test_path_mkdir_is_a_noop(patched: EditSession, project: Path) -> None:
    Path("src/brand_new").mkdir()
    assert not (project / "src" / "brand_new").exists()
    Path("src/brand_new/deep").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        Path("src").mkdir()


def test_path_mkdir_without_parents_raises(patched: EditSession, project: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Path("missing_parent/child").mkdir()


def test_path_rmdir(session: EditSession, project: Path) -> None:
    (project / "empty_dir").mkdir()
    restore = vfs.install(session)
    try:
        Path("empty_dir").rmdir()
        assert (project / "empty_dir").exists()
        with pytest.raises(OSError):
            Path("src").rmdir()
    finally:
        restore()


def test_path_rename_and_replace(patched: EditSession, project: Path) -> None:
    Path("src/a.py").rename("src/c.py")
    staged = patched.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "c.py"] == "alpha = 1\nbeta = 2\n"
    Path("src/b.py").replace("src/d.py")
    assert patched.staged()[project / "src" / "b.py"] is None


# --- os ---


def test_os_remove_and_rename(patched: EditSession, project: Path) -> None:
    os.remove("src/a.py")
    os.rename("src/b.py", "src/c.py")
    staged = patched.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "b.py"] is None
    assert staged[project / "src" / "c.py"] == "gamma = 3\n"


def test_os_rename_onto_dir_raises(patched: EditSession, project: Path) -> None:
    with pytest.raises(IsADirectoryError):
        os.rename("src/a.py", "src")


def test_os_remove_missing_raises(patched: EditSession) -> None:
    with pytest.raises(FileNotFoundError):
        os.remove("src/nope.py")


def test_os_makedirs_and_mkdir(patched: EditSession, project: Path) -> None:
    os.makedirs("a/b/c", exist_ok=True)
    assert not (project / "a").exists()
    os.mkdir("top")
    with pytest.raises(FileExistsError):
        os.makedirs("src", exist_ok=False)


def test_os_listdir_merges(patched: EditSession, project: Path) -> None:
    Path("src/new.py").write_text("x\n")
    os.remove("src/a.py")
    assert os.listdir("src") == ["b.py", "new.py"]


def test_os_walk_merges(session: EditSession, project: Path) -> None:
    (project / "src" / "sub").mkdir()
    restore = vfs.install(session)
    try:
        Path("src/sub/deep.py").write_text("x\n")
        Path("src/a.py").unlink()
        walk = {
            root: (sorted(dirs), sorted(files)) for root, dirs, files in os.walk("src")
        }
        assert walk[str(project / "src")] == (["sub"], ["b.py"])
        assert walk[str(project / "src" / "sub")] == ([], ["deep.py"])
    finally:
        restore()


def test_os_path_helpers_overlay_aware(patched: EditSession, project: Path) -> None:
    Path("src/new.py").write_text("x\n")
    Path("src/a.py").unlink()
    assert os.path.exists("src/new.py")
    assert os.path.isfile("src/new.py")
    assert not os.path.exists("src/a.py")
    assert os.path.isdir("src")


# --- shutil ---


def test_shutil_copy_and_copy2(patched: EditSession, project: Path) -> None:
    shutil.copy("src/a.py", "src/copy_a.py")
    shutil.copy2("src/a.py", "src/copy2_a.py")
    staged = patched.staged()
    assert staged[project / "src" / "copy_a.py"] == "alpha = 1\nbeta = 2\n"
    assert staged[project / "src" / "copy2_a.py"] == "alpha = 1\nbeta = 2\n"


def test_shutil_copyfile_rejects_dir_target(patched: EditSession) -> None:
    with pytest.raises(IsADirectoryError):
        shutil.copyfile("src/a.py", "src")


def test_shutil_copytree(patched: EditSession, project: Path) -> None:
    shutil.copytree("src", "copy_of_src")
    staged = {patched.relpath(p): c for p, c in patched.staged().items()}
    assert staged["copy_of_src/a.py"] == "alpha = 1\nbeta = 2\n"
    assert staged["copy_of_src/b.py"] == "gamma = 3\n"


def test_shutil_move(patched: EditSession, project: Path) -> None:
    shutil.move("src/a.py", "moved_a.py")
    staged = patched.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "moved_a.py"] == "alpha = 1\nbeta = 2\n"


def test_shutil_rmtree(patched: EditSession, project: Path) -> None:
    Path("src/sub").mkdir()
    Path("src/sub/deep.py").write_text("x\n")
    shutil.rmtree("src")
    staged = patched.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "b.py"] is None
    assert staged[project / "src" / "sub" / "deep.py"] is None


def test_shutil_rmtree_stages_new_files_too(
    patched: EditSession, project: Path
) -> None:
    Path("src/sub/deep.py").write_text("x\n")
    shutil.rmtree("src/sub")
    assert patched.staged()[project / "src" / "sub" / "deep.py"] is None


# --- diff integration ---


def test_binary_diff_is_a_note(patched: EditSession, project: Path) -> None:
    Path("src/a.bin").write_bytes(b"\x00\x01\x02")
    results = unified_diffs(patched.staged())
    rel, diff = results[0]
    assert rel == "src/a.bin"
    assert diff == "Binary file src/a.bin created (3 bytes)\n"


def test_restore_returns_stdlib_to_normal(session: EditSession, project: Path) -> None:
    restore = vfs.install(session)
    restore()
    target = project / "real_write.txt"
    Path(target).write_text("real\n")
    assert target.read_text() == "real\n"
    assert target.is_file()
    os.listdir(project)
