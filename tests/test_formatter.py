"""The formatter pass, driven through the CLI: stdout re-staged as the
final content, failures fail closed.

A stub formatter (stdin to stdout) stands in for nixfmt and ruff, so
the suite never needs either binary.
"""

import re
import sys
from pathlib import Path

import pytest

from pyedit import cli


@pytest.fixture
def upper(tmp_path: Path):
    """A stub formatter: uppercases its stdin. Returns the argv."""
    stub = tmp_path / "upper.py"
    stub.write_text("import sys\nsys.stdout.write(sys.stdin.read().upper())\n")
    return [sys.executable, str(stub)]


def configure(project: Path, **suffixes: list[str]):
    lines = ["[format]"]
    for suffix, argv in suffixes.items():
        lines.append(f"{suffix} = {argv!r}")
    (project / "pyedit.toml").write_text("\n".join(lines) + "\n")


def run(project: Path, *argv: object, script: Path | None = None):
    args = [str(a) for a in argv]
    if script is not None:
        args = ["--script", str(script), *args]
    return cli.main(args)


def undo_token(out: str) -> str:
    match = re.search(r"# pyedit undo ([0-9a-f]{8})", out)
    assert match, out
    return match.group(1)


@pytest.fixture
def script(project: Path):
    path = project / "edit.py"
    path.write_text('pyedit.edit("src/a.py", "alpha", "beta")\n')
    return path


def test_dry_run_formats_the_edited_text(
    project: Path, script: Path, upper: list[str], capsys: pytest.CaptureFixture[str]
):
    configure(project, py=upper)
    assert run(project, script=script) == 0
    out, err = capsys.readouterr()
    # the formatter saw the edited text, not disk truth
    assert "-alpha = 1" in out
    assert "+BETA = 1" in out
    assert "formatted: src/a.py (py)" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_apply_writes_formatted_and_undo_restores(
    project: Path, script: Path, upper: list[str], capsys: pytest.CaptureFixture[str]
):
    original = (project / "src" / "a.py").read_text()
    configure(project, py=upper)
    assert run(project, "--apply", script=script) == 0
    out = capsys.readouterr().out
    assert (project / "src" / "a.py").read_text() == "BETA = 1\nBETA = 2\n"
    assert run(project, "apply", undo_token(out)) == 0
    assert (project / "src" / "a.py").read_text() == original


def test_dry_run_id_replays_the_formatted_text(
    project: Path, script: Path, upper: list[str], capsys: pytest.CaptureFixture[str]
):
    configure(project, py=upper)
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    match = re.search(r"# pyedit dry-run ([0-9a-f]{8})", out)
    assert match is not None
    token = match.group(1)
    assert run(project, "apply", token) == 0
    assert (project / "src" / "a.py").read_text() == "BETA = 1\nBETA = 2\n"


def test_formatter_failure_fails_closed(
    project: Path, script: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    configure(project, py=[sys.executable, str(bad)])
    assert run(project, script=script) == 1
    err = capsys.readouterr().err
    assert "exit 3" in err
    assert "boom" in err
    assert "nothing was written" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_missing_formatter_binary_is_loud(
    project: Path, script: Path, capsys: pytest.CaptureFixture[str]
):
    configure(project, py=["pyedit-no-such-formatter"])
    assert run(project, script=script) == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "pyedit-no-such-formatter" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_formatter_without_stdout_is_rejected(
    project: Path, script: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    sink = tmp_path / "sink.py"
    sink.write_text("import sys\nsys.stdin.read()\n")
    configure(project, py=[sys.executable, str(sink)])
    assert run(project, script=script) == 1
    assert "no stdin mode" in capsys.readouterr().err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_formatter_reverting_to_disk_drops_the_file(
    project: Path, script: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    # the stub ignores stdin and echoes the file as it lies on disk:
    # script change + formatter revert = no net change, no diff
    revert = tmp_path / "revert.py"
    revert.write_text("import sys\nsys.stdout.write(open(sys.argv[1]).read())\n")
    configure(project, py=[sys.executable, str(revert), "{path}"])
    assert run(project, script=script) == 0
    out, err = capsys.readouterr()
    assert "no changes" in err
    assert "a.py" not in out


def test_only_configured_suffixes_are_formatted(
    project: Path, script: Path, upper: list[str], capsys: pytest.CaptureFixture[str]
):
    configure(project, py=upper)
    script.write_text(
        'pyedit.edit("src/a.py", "alpha", "beta")\n'
        'pyedit.edit("docs/note.txt", "hello", "HELLO")\n'
    )
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert "+BETA = 1" in out
    # txt has no configured formatter: it keeps its edited case
    assert "+HELLO" in out


def test_bytes_are_never_formatted(
    project: Path, script: Path, upper: list[str], capsys: pytest.CaptureFixture[str]
):
    configure(project, py=upper)
    script.write_text('pyedit.write("blob.py", b"x = 1\\x00\\n")\n')
    assert run(project, script=script) == 0
    assert "Binary file blob.py created" in capsys.readouterr().out


def test_path_placeholder_gets_the_absolute_path(
    project: Path, script: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    echo = tmp_path / "echo.py"
    echo.write_text(
        "import sys\nsys.stdout.write(sys.argv[1] + '|' + sys.stdin.read())\n"
    )
    configure(project, py=[sys.executable, str(echo), "{path}"])
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert f"+{project / 'src' / 'a.py'}|" in out


def test_formatter_config_from_config_home(
    project: Path,
    config_home: Path,
    script: Path,
    upper: list[str],
    capsys: pytest.CaptureFixture[str],
):
    lines = ["[format]", f"py = {upper!r}"]
    (config_home / "pyedit.toml").parent.mkdir(parents=True, exist_ok=True)
    (config_home / "pyedit.toml").write_text("\n".join(lines) + "\n")
    assert run(project, script=script) == 0
    assert "+BETA = 1" in capsys.readouterr().out
