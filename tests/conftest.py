import io

import pytest

from pyedit import store

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


@pytest.fixture(autouse=True)
def dryrun_store(tmp_path, monkeypatch):
    """Keep dry-run artifacts inside each test's tmp."""
    root = tmp_path / "store"
    monkeypatch.setattr(store, "store_dir", lambda: (root.mkdir(exist_ok=True), root)[1])
    return root


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Isolate pyedit.toml discovery inside each test's tmp, so a
    developer's own config-home file never leaks into a run."""
    home = tmp_path / "config-home"
    monkeypatch.setattr("pyedit.config.user_config_dir", lambda *a, **k: str(home))
    return home
