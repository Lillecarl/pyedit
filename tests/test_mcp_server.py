import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pyedit.mcp_server import run_edit


def edit(project: Path):
    return run_edit(
        script='pyedit.edit("src/a.py", "alpha", "ALPHA")',
        workdir=str(project),
        timeout=0,
    )


def test_dry_run_then_apply_id(project: Path):
    before = (project / "src" / "a.py").read_text()
    out = edit(project)
    assert out["ok"] is True, out
    assert "+ALPHA = 1" in out["diff"]
    assert out["dry_run_id"]
    assert out["applied"] is False
    assert (project / "src" / "a.py").read_text() == before

    back = run_edit(apply_id=out["dry_run_id"], workdir=str(project), timeout=0)
    assert back["ok"] is True, back
    assert back["applied"] is True
    assert back["undo_id"]
    assert "ALPHA = 1" in (project / "src" / "a.py").read_text()


def test_apply_with_include_matching_nothing_writes_nothing(project: Path):
    before = (project / "src" / "a.py").read_text()
    out = run_edit(
        script='pyedit.edit("src/a.py", "alpha", "ALPHA")',
        include=["nothing-matches-this"],
        apply=True,
        workdir=str(project),
        timeout=0,
    )
    assert out["ok"] is True, out
    assert out["files"] == []
    assert out["applied"] is False
    assert (project / "src" / "a.py").read_text() == before


def test_apply_id_replays_in_a_workdir_that_is_not_the_cwd(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    sub = project / "src"
    elsewhere = project / "docs"
    monkeypatch.chdir(elsewhere)
    out = run_edit(
        script='pyedit.edit("a.py", "alpha", "ALPHA")', workdir=str(sub), timeout=0
    )
    assert out["ok"] is True, out
    assert "--- a/a.py" in out["diff"]

    back = run_edit(apply_id=out["dry_run_id"], workdir=str(sub), timeout=0)
    assert back["ok"] is True, back
    assert (sub / "a.py").read_text() == "ALPHA = 1\nbeta = 2\n"

    # the id carries its root: no workdir needed to undo
    undo = run_edit(apply_id=back["undo_id"], timeout=0)
    assert undo["ok"] is True, undo
    assert (sub / "a.py").read_text() == "alpha = 1\nbeta = 2\n"
    assert Path.cwd() == elsewhere


def test_apply_id_replays_a_path_outside_the_workdir(project: Path):
    note = project / "docs" / "note.txt"
    out = run_edit(
        script=f"pyedit.edit({str(note)!r}, 'hello', 'HELLO')",
        workdir=str(project / "src"),
        timeout=0,
    )
    assert out["ok"] is True, out
    back = run_edit(apply_id=out["dry_run_id"], workdir=str(project / "src"), timeout=0)
    assert back["ok"] is True, back
    assert note.read_text() == "HELLO\n"


def test_apply_id_replays_a_dry_run_shown_with_no_context(project: Path):
    big = project / "big.py"
    big.write_text("".join(f"line{i} = {i}\n" for i in range(40)))
    script = (
        "pyedit.edit('big.py', 'line5 = 5\\nline6 = 6\\n', '')\n"
        "pyedit.edit('big.py', 'line30 = 30\\n', 'line30 = 300\\nextra = 1\\n')\n"
        "pyedit.write('new.py', 'x = 1\\n')\n"
    )
    out = run_edit(script=script, context=0, workdir=str(project), timeout=0)
    assert out["ok"] is True, out
    assert "@@ -6,2 +5,0 @@" in out["diff"]
    back = run_edit(apply_id=out["dry_run_id"], workdir=str(project), timeout=0)
    assert back["ok"] is True, back
    assert "line5" not in big.read_text()
    assert "line30 = 300\nextra = 1\n" in big.read_text()


def test_output_writes_the_diff_to_a_file(project: Path):
    out = run_edit(
        script='pyedit.edit("a.py", "alpha", "ALPHA")\npyedit.write("new.txt", "x\\ny\\n")',
        output="changes.diff",
        workdir=str(project / "src"),
        timeout=0,
    )
    assert out["ok"] is True, out
    assert "diff" not in out
    assert out["diff_file"] == str(project / "src" / "changes.diff")
    assert "+ALPHA = 1" in (project / "src" / "changes.diff").read_text()
    assert out["stat"] == [["a.py", 1, 1], ["new.txt", 2, 0]]
    assert out["dry_run_id"]


def test_refused_apply_honours_output(project: Path):
    out = run_edit(
        script='pyedit.write("broken.py", "def f(:\\n")',
        output="changes.diff",
        apply=True,
        workdir=str(project),
        timeout=0,
    )
    assert out["ok"] is False
    assert "diff" not in out
    assert "broken.py" in (project / "changes.diff").read_text()
    assert not (project / "broken.py").exists()


def test_line_counts_skip_headers_only():
    from pyedit.diff import line_counts

    section = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n--- removed dashes\n+new\n keep\n"
    assert line_counts(section) == (1, 1)
    assert line_counts("Binary file x changed (1 -> 2 bytes)\n") == (0, 0)


def test_fds_replace_heredocs(project: Path):
    (project / "esc.py").write_text('rx = "\\d+"\n')
    out = run_edit(
        script='pyedit.edit("esc.py", pyedit.read_fd(3), pyedit.read_fd(4))',
        fds={"3": 'rx = "\\d+"\n', "4": 'rx = r"\\d+"\n'},
        workdir=str(project),
        apply=True,
        timeout=0,
    )
    assert out["ok"] is True, out
    assert (project / "esc.py").read_text() == 'rx = r"\\d+"\n'


def test_script_file_relative_and_missing(project: Path):
    (project / "refactor.py").write_text('pyedit.edit("src/a.py", "alpha", "ALPHA")\n')
    out = run_edit(script_file="refactor.py", workdir=str(project), timeout=0)
    assert out["ok"] is True, out
    assert "+ALPHA = 1" in out["diff"]

    missing = run_edit(script_file="nope.py", workdir=str(project), timeout=0)
    assert missing["ok"] is False
    assert "nope.py" in missing["error"]


def test_exactly_one_source(project: Path):
    assert run_edit(timeout=0)["ok"] is False
    both = run_edit(script="x", script_file="y", workdir=str(project), timeout=0)
    assert both["ok"] is False
    assert "exactly one" in both["error"]


def test_rejections(project: Path):
    assert "no stored dry-run" in run_edit(apply_id="deadbeef")["error"]
    assert (
        "not a directory"
        in run_edit(script="x", workdir=str(project / "missing"), timeout=0)["error"]
    )
    assert (
        "FD numbers"
        in run_edit(script="x", fds={"old": "y"}, workdir=str(project), timeout=0)[
            "error"
        ]
    )
    assert (
        "must be str"
        in run_edit(
            script="x",
            fds={"3": 4},  # pyrefly: ignore[bad-assignment] int value on purpose
            workdir=str(project),
            timeout=0,
        )["error"]
    )
    assert "lines" in run_edit(script="x", context=-1, timeout=0)["error"]


def test_syntax_problems_are_structured(project: Path):
    out = run_edit(
        script='pyedit.write("broken.py", "def f(:\\n")',
        workdir=str(project),
        timeout=0,
    )
    assert out["ok"] is True, out
    assert out["dry_run_id"]
    assert any("broken.py" in p for p in out["problems"])
    assert not (project / "broken.py").exists()


def test_apply_with_syntax_problems_refuses(project: Path):
    out = run_edit(
        script='pyedit.write("broken.py", "def f(:\\n")',
        workdir=str(project),
        apply=True,
        timeout=0,
    )
    assert out["ok"] is False
    assert "syntax check failed" in out["error"]
    assert "broken.py" in out["diff"]
    assert not (project / "broken.py").exists()


def test_script_errors_carry_a_traceback(project: Path):
    out = run_edit(script="raise ValueError('boom')", workdir=str(project), timeout=0)
    assert out["ok"] is False
    assert "ValueError: boom" in out["error"]


def test_script_raising_system_exit_fails_the_run_only(project: Path):
    out = run_edit(
        script="pyedit.edit('src/a.py', 'alpha', 'ALPHA')\nraise SystemExit('probe: 42')",
        workdir=str(project),
        apply=True,
        timeout=0,
    )
    assert out["ok"] is False
    assert "SystemExit: probe: 42" in out["error"]
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


@pytest.fixture
def child_env(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A child process sees none of conftest's monkeypatches: isolate it
    through the environment it is shipped instead."""
    (project / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(project / "tmp"))
    monkeypatch.setenv("XDG_STATE_HOME", str(project / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "config"))
    (project / "note.txt").write_text("hello\n")
    return project


def test_child_run_matches_in_process(child_env: Path):
    from pyedit.mcp_server import run_in_child

    out = run_in_child(
        script="print('probe', 42)\npyedit.edit('note.txt', 'hello', 'HELLO')",
        apply=True,
        timeout=30,
    )
    assert out["ok"] is True, out
    assert out["stdout"] == "probe 42\n"
    assert out["stat"] == [["note.txt", 1, 1]]
    assert (child_env / "note.txt").read_text() == "HELLO\n"
    assert out["undo_id"]
    assert (child_env / "tmp" / "pyedit-dryruns" / f"{out['undo_id']}.diff").exists()


def test_child_timeout_fails_only_that_run(child_env: Path):
    from pyedit.mcp_server import run_in_child

    out = run_in_child(script="import time\ntime.sleep(30)", timeout=1)
    assert out["ok"] is False
    assert "timed out after 1s; thread stacks in" in out["error"]
    assert "nothing was written" in out["error"]
    assert list((child_env / "state" / "pyedit" / "dumps").glob("timeout-*.log"))
    after = run_in_child(script="pyedit.edit('note.txt', 'hello', 'HELLO')", timeout=30)
    assert after["ok"] is True, after


def test_child_stuck_holding_the_gil_is_killed(
    child_env: Path, monkeypatch: pytest.MonkeyPatch
):
    from pyedit import mcp_server

    monkeypatch.setattr(mcp_server, "_GRACE", 1.0)

    # a backtracking C regex holds the GIL, so the child's own watchdog
    # never runs: only the parent's deadline ends it
    script = "import re\nre.match(r'(a+)+$', 'a' * 40 + 'b')"
    started = time.monotonic()
    out = mcp_server.run_in_child(script=script, timeout=1)
    assert out["ok"] is False
    assert "did not stop for the watchdog" in out["error"]
    assert time.monotonic() - started < 1 + mcp_server._GRACE + 5


def test_skill_documents_mcp():
    from pyedit.skill import render_skill

    text = render_skill()
    assert "pyedit mcp" in text
    assert "apply_id" in text


def _request(line_id: int, method: str, params: dict[str, object] | None = None):
    message: dict[str, object] = {"jsonrpc": "2.0", "id": line_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message) + "\n"


def test_stdio_serves_a_single_run_tool(project: Path):
    pytest.importorskip("mcp")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pyedit", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=project,
    )
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pyedit-test", "version": "0"},
                },
            )
        )
        proc.stdin.flush()
        hello = json.loads(proc.stdout.readline())
        assert hello["result"]["serverInfo"]["name"] == "pyedit"

        proc.stdin.write('{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        proc.stdin.write(_request(2, "tools/list"))
        proc.stdin.flush()
        listing = json.loads(proc.stdout.readline())
        assert [t["name"] for t in listing["result"]["tools"]] == ["run"]

        proc.stdin.write(
            _request(
                3,
                "tools/call",
                {
                    "name": "run",
                    "arguments": {
                        "script": 'pyedit.edit("src/a.py", "alpha", "ALPHA")',
                        "workdir": str(project),
                    },
                },
            )
        )
        proc.stdin.flush()
        called = json.loads(proc.stdout.readline())
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert "+ALPHA = 1" in payload["diff"]
        assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"
    finally:
        proc.stdin.close()
        _, stderr = proc.communicate(timeout=60)
    assert "Traceback" not in stderr, stderr
