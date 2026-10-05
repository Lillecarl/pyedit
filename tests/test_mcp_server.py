import json
import subprocess
import sys

import pytest

from pyedit.mcp_server import run_edit


def edit(project):
    return run_edit(
        script='pyedit.edit("src/a.py", "alpha", "ALPHA")',
        workdir=str(project),
        timeout=0,
    )


def test_dry_run_then_apply_id(project):
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


def test_fds_replace_heredocs(project):
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


def test_script_file_relative_and_missing(project):
    (project / "refactor.py").write_text('pyedit.edit("src/a.py", "alpha", "ALPHA")\n')
    out = run_edit(script_file="refactor.py", workdir=str(project), timeout=0)
    assert out["ok"] is True, out
    assert "+ALPHA = 1" in out["diff"]

    missing = run_edit(script_file="nope.py", workdir=str(project), timeout=0)
    assert missing["ok"] is False
    assert "nope.py" in missing["error"]


def test_exactly_one_source(project):
    assert run_edit(timeout=0)["ok"] is False
    both = run_edit(script="x", script_file="y", workdir=str(project), timeout=0)
    assert both["ok"] is False
    assert "exactly one" in both["error"]


def test_rejections(project):
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
        in run_edit(script="x", fds={"3": 4}, workdir=str(project), timeout=0)["error"]
    )
    assert "lines" in run_edit(script="x", context=-1, timeout=0)["error"]


def test_syntax_problems_are_structured(project):
    out = run_edit(
        script='pyedit.write("broken.py", "def f(:\\n")',
        workdir=str(project),
        timeout=0,
    )
    assert out["ok"] is True, out
    assert out["dry_run_id"]
    assert any("broken.py" in p for p in out["problems"])
    assert not (project / "broken.py").exists()


def test_apply_with_syntax_problems_refuses(project):
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


def test_script_errors_carry_a_traceback(project):
    out = run_edit(script="raise ValueError('boom')", workdir=str(project), timeout=0)
    assert out["ok"] is False
    assert "ValueError: boom" in out["error"]


def test_skill_documents_mcp():
    from pyedit.skill import render_skill

    text = render_skill()
    assert "pyedit mcp" in text
    assert "apply_id" in text


def _request(line_id, method, params=None):
    message: dict = {"jsonrpc": "2.0", "id": line_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message) + "\n"


def test_stdio_serves_a_single_run_tool(project):
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
