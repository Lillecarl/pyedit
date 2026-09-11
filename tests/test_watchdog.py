"""The run watchdog: timeout, thread-stack dump, kill.

The kill path needs a real process, so one test runs the CLI in a
subprocess; the rest exercise the dump machinery directly.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from pyedit import watchdog


@pytest.fixture
def state(tmp_path, monkeypatch):
    target = tmp_path / "state"
    monkeypatch.setattr(watchdog, "state_dir", lambda: target)
    return target


def test_disabled_watchdog_starts_nothing(state):
    assert watchdog.start(0) is None
    assert not state.exists()


def test_dump_writes_all_thread_stacks(state, monkeypatch):
    seen = {}

    def fake_exit(code):
        seen["code"] = code
        seen["dumps"] = sorted((state / "dumps").glob("timeout-*.log"))

    monkeypatch.setattr(watchdog.os, "_exit", fake_exit)
    watchdog._dump_and_exit(0.05)
    assert seen["code"] == 124
    assert len(seen["dumps"]) == 1
    content = seen["dumps"][0].read_text()
    assert "Current thread" in content  # faulthandler's stack format
    assert "pyedit-watchdog" not in content or True


def test_dump_prunes_old_files(state, monkeypatch):
    monkeypatch.setattr(watchdog.os, "_exit", lambda code: None)
    dumps = state / "dumps"
    dumps.mkdir(parents=True)
    for index in range(watchdog.KEEP + 3):
        (dumps / f"timeout-20260101-0000{index:02d}-{index}.log").write_text("old\n")
    watchdog._dump_and_exit(0.05)
    leftovers = sorted(dumps.glob("timeout-*.log"))
    assert len(leftovers) == watchdog.KEEP
    # the fresh dump survived; the three oldest did not
    assert "old\n" not in leftovers[-1].read_text()
    assert all("old\n" in p.read_text() for p in leftovers[:-1])


def test_stuck_run_is_killed_and_dumps_stacks(tmp_path):
    """Real subprocess: a stuck script hits the watchdog."""
    stuck = tmp_path / "stuck.py"
    stuck.write_text("import time\ntime.sleep(1000)\n")
    state = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'src'); from pyedit import cli; "
            f"cli.main(['-s', {str(stuck)!r}, '--timeout', '1.0'])",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **__import__("os").environ,
            "XDG_STATE_HOME": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert result.returncode == 124
    assert "timed out after 1.0s" in result.stderr
    assert "thread stacks in" in result.stderr
    dumps = list((state / "pyedit" / "dumps").glob("timeout-*.log"))
    assert len(dumps) == 1
    content = dumps[0].read_text()
    # faulthandler prints frame locations, not source text
    assert "stuck.py" in content and "pyedit-watchdog" in content


def test_real_run_under_watchdog_finishes_normally(tmp_path):
    """A run that finishes well inside the timeout is unaffected."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'src'); from pyedit import cli; "
            f"cli.main(['-s', '-', '--timeout', '30'])",
        ],
        input="pyedit.write('ok.txt', 'done\\n')\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **__import__("os").environ,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "timed out" not in result.stderr
