import io

import pytest

_real_open = io.open


@pytest.fixture
def disk_text():
    """Read the real file on disk, bypassing any VFS patches."""

    def _read(path) -> str:
        with _real_open(path) as f:
            return f.read()

    return _read


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\nbeta = 2\n")
    (tmp_path / "src" / "b.py").write_text("gamma = 3\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.txt").write_text("hello\n")
    return tmp_path
