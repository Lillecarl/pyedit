import io
import subprocess
import sys
from pathlib import Path

import pytest

from pyedit import cli

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\n")
    (tmp_path / "src" / "b.py").write_text("beta = 2\n")
    return tmp_path


@pytest.fixture
def script(project):
    path = project / "edit.py"
    path.write_text('pyedit.edit("src/a.py", "alpha", "ALPHA")\n')
    return path


def run(project, *argv, script=None):
    argv = [str(a) for a in argv]
    if script is not None:
        argv = ["--script", str(script), *argv]
    return cli.main(argv)


def test_dry_run_prints_diff_and_writes_nothing(project, script, capsys):
    before = (project / "src" / "a.py").read_text()
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert "--- a/src/a.py" in out
    assert "+ALPHA = 1" in out
    assert (project / "src" / "a.py").read_text() == before


def test_apply_writes_changes(project, script, capsys):
    assert run(project, "--apply", script=script) == 0
    assert (project / "src" / "a.py").read_text() == "ALPHA = 1\n"
    assert "--- a/src/a.py" in capsys.readouterr().out


def test_output_writes_diff_to_file(project, script, capsys):
    target = project / "changes.diff"
    assert run(project, "--output", target, script=script) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "--- a/src/a.py" in target.read_text()


def test_no_changes_is_quiet_on_stdout(project, capsys):
    quiet = project / "noop.py"
    quiet.write_text("pass\n")
    assert run(project, script=quiet) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "no changes" in err


def test_include_filter_limits_diff_and_apply(project, capsys):
    script = project / "edit.py"
    script.write_text(
        'pyedit.edit("src/a.py", "alpha", "ALPHA")\n'
        'pyedit.edit("src/b.py", "beta", "BETA")\n'
    )
    assert run(project, "--include", "src/b.py", "--apply", script=script) == 0
    out = capsys.readouterr().out
    assert "src/b.py" in out
    assert "src/a.py" not in out
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"
    assert (project / "src" / "b.py").read_text() == "BETA = 2\n"


def test_exclude_filter_limits_diff_and_apply(project, capsys):
    script = project / "edit.py"
    script.write_text(
        'pyedit.edit("src/a.py", "alpha", "ALPHA")\n'
        'pyedit.edit("src/b.py", "beta", "BETA")\n'
    )
    assert run(project, "--exclude", "*.py", script=script) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "no changes" in err
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"


def test_failing_script_returns_1_and_writes_nothing(project, capsys):
    script = project / "bad.py"
    script.write_text("pyedit.edit('src/a.py', 'missing', 'x')\n")
    before = (project / "src" / "a.py").read_text()
    assert run(project, script=script) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "nothing was written" in err
    assert (project / "src" / "a.py").read_text() == before


def test_apply_creates_new_file(project, capsys):
    script = project / "edit.py"
    script.write_text('pyedit.write("src/new.py", "fresh = 1\\n")\n')
    assert run(project, "--apply", script=script) == 0
    assert (project / "src" / "new.py").read_text() == "fresh = 1\n"


def test_apply_deletes_file(project, capsys):
    script = project / "edit.py"
    script.write_text('pyedit.delete("src/a.py")\n')
    assert run(project, "--apply", script=script) == 0
    assert not (project / "src" / "a.py").exists()


def test_dry_run_does_not_delete(project, capsys):
    script = project / "edit.py"
    script.write_text('pyedit.delete("src/a.py")\n')
    assert run(project, script=script) == 0
    assert (project / "src" / "a.py").exists()


def test_script_can_touch_any_path(project, script, capsys):
    script.write_text(
        'pyedit.write("outside/the_input.py", "anywhere\\n")\n'
    )
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert "+++ b/outside/the_input.py" in out
    assert not (project / "outside").exists()


def test_stdin_script_end_to_end(project):
    result = subprocess.run(
        [sys.executable, "-m", "pyedit"],
        input='pyedit.edit("src/a.py", "alpha", "ALPHA")\n',
        capture_output=True,
        text=True,
        cwd=project,
        env={"PYTHONPATH": str(SRC), "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "+ALPHA = 1" in result.stdout
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"


def test_ordinary_python_script_is_captured(project, capsys):
    script = project / "edit.py"
    script.write_text(
        "from pathlib import Path\n"
        "import os, shutil\n"
        "for p in Path('src').glob('*.py'):\n"
        "    p.write_text(p.read_text().replace('alpha', 'ALPHA'))\n"
        "shutil.copy('src/a.py', 'src/a.py.bak')\n"
        "os.remove('src/b.py')\n"
    )
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert "+ALPHA = 1" in out
    assert "src/a.py.bak" in out
    assert "src/b.py" in out
    assert (project / "src" / "b.py").exists()
    assert not (project / "src" / "a.py.bak").exists()


def test_ordinary_python_apply_writes(project, capsys):
    script = project / "edit.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('src/brand_new/f.txt').write_text('fresh\\n')\n"
    )
    assert run(project, "--apply", script=script) == 0
    assert (project / "src" / "brand_new" / "f.txt").read_text() == "fresh\n"


def test_binary_apply_writes_bytes(project, capsys):
    script = project / "edit.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('src/data.bin').write_bytes(bytes([0, 1, 2]))\n"
    )
    assert run(project, "--apply", script=script) == 0
    assert (project / "src" / "data.bin").read_bytes() == bytes([0, 1, 2])
    out = capsys.readouterr().out
    assert "Binary file src/data.bin created (3 bytes)" in out


def test_import_and_global_session_are_equivalent(project, capsys):
    script = project / "edit.py"
    script.write_text(
        "import pyedit\n"
        "assert pyedit.session is not None\n"
        "pyedit.glob('**/*.py')\n"
        "pyedit.session.edit('src/a.py', 'alpha', 'ALPHA')\n"
    )
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    assert "+ALPHA = 1" in out


def test_patch_file_mode(project, capsys):
    patch = project / "plan.patch"
    patch.write_text(
        "*** Begin Patch\n"
        "*** Update File: src/a.py\n"
        "@@\n"
        "-alpha = 1\n"
        "+alpha = 42\n"
        "*** End Patch\n"
    )
    assert run(project, "--patch", patch) == 0
    out = capsys.readouterr().out
    assert "+alpha = 42" in out
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"


def test_patch_stdin_auto_detect(project, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "*** Begin Patch\n"
            "*** Add File: notes/todo.txt\n"
            "+write tests\n"
            "*** End Patch\n"
        ),
    )
    assert run(project) == 0
    out = capsys.readouterr().out
    assert "+++ b/notes/todo.txt" in out
    assert not (project / "notes" / "todo.txt").exists()


def test_patch_apply_writes(project, capsys):
    patch = project / "plan.patch"
    patch.write_text(
        "*** Begin Patch\n"
        "*** Delete File: src/b.py\n"
        "*** End Patch\n"
    )
    assert run(project, "--patch", patch, "--apply") == 0
    assert not (project / "src" / "b.py").exists()


def test_diff_file_mode(project, capsys):
    diff = project / "plan.diff"
    diff.write_text(
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-alpha = 1\n"
        "+alpha = 42\n"
    )
    assert run(project, "--diff", diff) == 0
    out = capsys.readouterr().out
    assert "+alpha = 42" in out
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"


def test_diff_stdin_auto_detect(project, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1 +1 @@\n"
            "-alpha = 1\n"
            "+alpha = 42\n"
        ),
    )
    assert run(project) == 0
    assert "+alpha = 42" in capsys.readouterr().out


def test_diff_apply_writes(project, capsys):
    diff = project / "plan.diff"
    diff.write_text(
        "--- /dev/null\n+++ b/src/fresh.txt\n@@ -0,0 +1 @@\n+new\n"
    )
    assert run(project, "--diff", diff, "--apply") == 0
    assert (project / "src" / "fresh.txt").read_text() == "new\n"


def test_patch_inline_from_script(project, capsys):
    script = project / "edit.py"
    script.write_text(
        'pyedit.apply_patch("*** Begin Patch\\n"\n'
        '                 "*** Update File: src/a.py\\n"\n'
        '                 "@@ alpha\\n"\n'
        '                 "-alpha = 1\\n"\n'
        '                 "+alpha = 7\\n"\n'
        '                 "*** End Patch\\n")\n'
    )
    assert run(project, script=script) == 0
    assert "+alpha = 7" in capsys.readouterr().out


def test_patch_failure_returns_1(project, capsys):
    patch = project / "plan.patch"
    patch.write_text(
        "*** Begin Patch\n"
        "*** Update File: src/missing.py\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    assert run(project, "--patch", patch) == 1
    _, err = capsys.readouterr()
    assert "nothing was written" in err


def test_skill_prints_markdown(capsys):
    assert cli.main(["skill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# pyedit")
    assert "Nothing touches disk" in out
    assert "--apply" in out


def test_skill_writes_file(project, capsys):
    target = project / "docs" / "skill.md"
    assert cli.main(["skill", str(target)]) == 0
    assert capsys.readouterr().out == ""
    assert target.read_text().startswith("# pyedit")
