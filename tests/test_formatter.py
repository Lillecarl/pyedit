"""The formatter pass, driven through the CLI: stdout re-staged as the
final content, failures fail closed.

A stub formatter (stdin to stdout) stands in for nixfmt and ruff, so
the suite never needs either binary.
"""

import re
import sys

import pytest

from pyedit import cli


@pytest.fixture
def upper(tmp_path):
    """A stub formatter: uppercases its stdin. Returns the argv."""
    stub = tmp_path / "upper.py"
    stub.write_text("import sys\nsys.stdout.write(sys.stdin.read().upper())\n")
    return [sys.executable, str(stub)]


def configure(project, **suffixes):
    lines = ["[format]"]
    for suffix, argv in suffixes.items():
        lines.append(f"{suffix} = {argv!r}")
    (project / "pyedit.toml").write_text("\n".join(lines) + "\n")


def run(project, *argv, script=None):
    argv = [str(a) for a in argv]
    if script is not None:
        argv = ["--script", str(script), *argv]
    return cli.main(argv)


def undo_token(out: str) -> str:
    match = re.search(r"# pyedit undo ([0-9a-f]{8})", out)
    assert match, out
    return match.group(1)


@pytest.fixture
def script(project):
    path = project / "edit.py"
    path.write_text('pyedit.edit("src/a.py", "alpha", "beta")\n')
    return path


def test_dry_run_formats_the_edited_text(project, script, upper, capsys):
    configure(project, py=upper)
    assert run(project, script=script) == 0
    out, err = capsys.readouterr()
    # the formatter saw the edited text, not disk truth
    assert "-alpha = 1" in out
    assert "+BETA = 1" in out
    assert "formatted: src/a.py (py)" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_apply_writes_formatted_and_undo_restores(project, script, upper, capsys):
    original = (project / "src" / "a.py").read_text()
    configure(project, py=upper)
    assert run(project, "--apply", script=script) == 0
    out = capsys.readouterr().out
    assert (project / "src" / "a.py").read_text() == "BETA = 1\nBETA = 2\n"
    assert run(project, "apply", undo_token(out)) == 0
    assert (project / "src" / "a.py").read_text() == original


def test_dry_run_id_replays_the_formatted_text(project, script, upper, capsys):
    configure(project, py=upper)
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    token = re.search(r"# pyedit dry-run ([0-9a-f]{8})", out).group(1)
    assert run(project, "apply", token) == 0
    assert (project / "src" / "a.py").read_text() == "BETA = 1\nBETA = 2\n"


def test_formatter_failure_fails_closed(project, script, tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    configure(project, py=[sys.executable, str(bad)])
    assert run(project, script=script) == 1
    err = capsys.readouterr().err
    assert "exit 3" in err
    assert "boom" in err
    assert "nothing was written" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_missing_formatter_binary_is_loud(project, script, capsys):
    configure(project, py=["pyedit-no-such-formatter"])
    assert run(project, script=script) == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "pyedit-no-such-formatter" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_formatter_without_stdout_is_rejected(project, script, tmp_path, capsys):
    sink = tmp_path / "sink.py"
    sink.write_text("import sys\nsys.stdin.read()\n")
    configure(project, py=[sys.executable, str(sink)])
    assert run(project, script=script) == 1
    assert "no stdin mode" in capsys.readouterr().err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\nbeta = 2\n"


def test_formatter_reverting_to_disk_drops_the_file(
    project, script, tmp_path, capsys
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


def test_only_configured_suffixes_are_formatted(project, script, upper, capsys):
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


def test_bytes_are_never_formatted(project, script, upper, capsys):
    configure(project, py=upper)
    script.write_text('pyedit.write("blob.py", b"x = 1\\x00\\n")\n')
    assert run(project, script=script) == 0
    assert "Binary file blob.py created" in capsys.readouterr().out


def test_path_placeholder_gets_the_absolute_path(project, script, tmp_path, capsys):
    echo = tmp_path / "echo.py"
    echo.write_text(
        "import sys\nsys.stdout.write(sys.argv[1] + '|' + sys.stdin.read())\n"
    )
    configure(project, py=[sys.executable, str(echo), "{path}"])
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert f"+{project / 'src' / 'a.py'}|" in out


def test_formatter_config_from_config_home(project, config_home, script, upper, capsys):
    lines = ["[format]", f"py = {upper!r}"]
    (config_home / "pyedit.toml").parent.mkdir(parents=True, exist_ok=True)
    (config_home / "pyedit.toml").write_text("\n".join(lines) + "\n")
    assert run(project, script=script) == 0
    assert "+BETA = 1" in capsys.readouterr().out
