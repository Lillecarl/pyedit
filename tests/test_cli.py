import io
import re
import sys
from pathlib import Path

import pytest

import pyedit
from pyedit import cli
from pyedit import store
from syntax_corpus import LANGUAGES


@pytest.fixture(autouse=True)
def dryrun_store(tmp_path, monkeypatch):
    """Keep dry-run artifacts inside each test's tmp."""
    root = tmp_path / "store"
    monkeypatch.setattr(store, "store_dir", lambda: (root.mkdir(exist_ok=True), root)[1])
    return root


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


def test_dry_run_prints_id_comments_and_stores_pure_diff(
    project, script, capsys, dryrun_store
):
    assert run(project, script=script) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    match = re.fullmatch(
        r"# pyedit dry-run ([0-9a-f]{8}) \(pyedit --apply \1 to apply\)", lines[0]
    )
    assert match, lines[0]
    token = match.group(1)
    assert lines[-1] == lines[0]
    stored = dryrun_store / f"{token}.diff"
    text = stored.read_text()
    # the stored patch is git-canonical so libgit2 replays it; the
    # printed diff is the readable rendering and has no such header
    assert text.startswith("diff --git a/src/a.py b/src/a.py")
    assert "# pyedit" not in text
    assert "diff --git" not in out
    assert f"saved as {token}" in err


def test_apply_from_stored_id(project, script, capsys):
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    token = re.search(r"# pyedit dry-run ([0-9a-f]{8})", out).group(1)
    assert run(project, "--apply", token) == 0
    assert (project / "src" / "a.py").read_text() == "ALPHA = 1\n"
    out = capsys.readouterr().out
    assert "+ALPHA = 1" in out
    assert "# pyedit dry-run" not in out


def undo_token(out: str) -> str:
    match = re.search(r"# pyedit undo ([0-9a-f]{8})", out)
    assert match, out
    return match.group(1)


def test_apply_prints_undo_comment(project, script, capsys, dryrun_store):
    assert run(project, "--apply", script=script) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    token = undo_token(out)
    assert lines[0] == lines[-1] == f"# pyedit undo {token} (pyedit --apply {token} to revert)"
    undo_text = (dryrun_store / f"{token}.diff").read_text()
    assert "-ALPHA = 1" in undo_text
    assert "+alpha = 1" in undo_text
    assert "undo saved as" in err


def test_undo_reverts_an_apply(project, script, capsys):
    original = (project / "src" / "a.py").read_text()
    assert run(project, "--apply", script=script) == 0
    out = capsys.readouterr().out
    token = undo_token(out)
    assert (project / "src" / "a.py").read_text() == "ALPHA = 1\n"
    assert run(project, "--apply", token) == 0
    assert (project / "src" / "a.py").read_text() == original


def test_stored_id_replay_of_create_heavy_diffs(project, capsys):
    script = project / "edit.py"
    body = "\n".join(
        f'pyedit.write("mod{i}.py", "line1\\nline2\\n")' for i in range(10)
    )
    script.write_text(body + "\n")
    assert run(project, script=script) == 0
    token = re.search(
        r"dry-run ([0-9a-f]{8})", capsys.readouterr().out
    ).group(1)
    assert run(project, "--apply", token) == 0
    assert all((project / f"mod{i}.py").exists() for i in range(10))


def test_undo_recreates_a_deleted_file(project, capsys):
    victim = project / "src" / "b.py"
    assert victim.read_text() == "beta = 2\n"
    deleter = project / "del.py"
    deleter.write_text('pyedit.delete("src/b.py")\n')
    assert run(project, "--apply", script=deleter) == 0
    assert not victim.exists()
    token = undo_token(capsys.readouterr().out)
    assert run(project, "--apply", token) == 0
    assert victim.read_text() == "beta = 2\n"


def test_undo_removes_a_created_file(project, capsys):
    creator = project / "new.py"
    creator.write_text('pyedit.write("src/fresh.txt", "made\\n")\n')
    assert run(project, "--apply", script=creator) == 0
    assert (project / "src" / "fresh.txt").read_text() == "made\n"
    token = undo_token(capsys.readouterr().out)
    assert run(project, "--apply", token) == 0
    assert not (project / "src" / "fresh.txt").exists()


def test_undo_reverses_a_rename(project, capsys):
    mover = project / "mv.py"
    mover.write_text('pyedit.rename("src/a.py", "src/renamed.py")\n')
    assert run(project, "--apply", script=mover) == 0
    assert not (project / "src" / "a.py").exists()
    assert (project / "src" / "renamed.py").read_text() == "alpha = 1\n"
    token = undo_token(capsys.readouterr().out)
    assert run(project, "--apply", token) == 0
    assert (project / "src" / "a.py").read_text() == "alpha = 1\n"
    assert not (project / "src" / "renamed.py").exists()


def test_stored_dry_run_replays_a_binary_change(project, capsys):
    (project / "data.bin").write_bytes(b"\x00\x01\x02")
    binary = project / "bin.py"
    binary.write_text('pyedit.write("data.bin", b"\\xff\\xfe")\n')
    assert run(project, script=binary) == 0
    out, _err = capsys.readouterr()
    # printed as a summary, stored with the payload
    assert "Binary file data.bin changed (3 -> 2 bytes)" in out
    assert "GIT binary patch" not in out
    assert (project / "data.bin").read_bytes() == b"\x00\x01\x02"

    token = re.search(r"# pyedit dry-run ([0-9a-f]{8})", out).group(1)
    assert run(project, "--apply", token) == 0
    assert (project / "data.bin").read_bytes() == b"\xff\xfe"


def test_symlink_retarget_undoes(project, capsys):
    (project / "link").symlink_to("src/a.py")
    script = project / "s.py"
    script.write_text('pyedit.symlink("src/b.py", "link", force=True)\n')
    assert run(project, "--apply", script=script) == 0
    out, _err = capsys.readouterr()
    assert "Symlink link retargeted (src/a.py -> src/b.py)" in out
    assert (project / "link").readlink().name == "b.py"

    assert run(project, "--apply", undo_token(out)) == 0
    assert (project / "link").readlink().name == "a.py"


def test_binary_create_undoes(project, capsys):
    binary = project / "bin.py"
    binary.write_text('pyedit.write("data.bin", b"\\x00\\x01\\x02")\n')
    assert run(project, "--apply", script=binary) == 0
    out, _err = capsys.readouterr()
    # the printed diff keeps its one-line summary; the stored undo
    # carries the payload that puts the bytes back
    assert "Binary file data.bin created (3 bytes)" in out
    assert "GIT binary patch" not in out
    assert (project / "data.bin").read_bytes() == b"\x00\x01\x02"

    assert run(project, "--apply", undo_token(out)) == 0
    assert not (project / "data.bin").exists()


def test_binary_change_undoes(project, capsys):
    (project / "data.bin").write_bytes(b"\x00\x01\x02")
    binary = project / "bin.py"
    binary.write_text('pyedit.write("data.bin", b"\\xff\\xfe")\n')
    assert run(project, "--apply", script=binary) == 0
    out, _err = capsys.readouterr()
    assert (project / "data.bin").read_bytes() == b"\xff\xfe"

    assert run(project, "--apply", undo_token(out)) == 0
    assert (project / "data.bin").read_bytes() == b"\x00\x01\x02"


def test_broken_syntax_warns_on_dry_run(project, capsys):
    broken = project / "bad.py"
    broken.write_text('pyedit.write("src/broken.py", "def (:\\n")\n')
    assert run(project, script=broken) == 0
    out, err = capsys.readouterr()
    assert "pyedit: syntax: src/broken.py:1:" in err
    # the source line with a caret, CPython style
    assert re.search(r"^\s+1 \| def \(:$", err, re.M)
    assert re.search(r"^\s+\| +\^$", err, re.M)
    assert "+def (:" in out  # the diff still shows what was staged


@pytest.mark.parametrize(
    "name,suffix,correct,broken,names",
    LANGUAGES,
    ids=[entry[0] for entry in LANGUAGES],
)
def test_broken_syntax_gate_refuses_then_force_applies(
    name, suffix, correct, broken, names, project, capsys
):
    target = f"src/sample{suffix}"
    script = project / "bad.py"
    script.write_text(f"pyedit.write({target!r}, {broken!r})\n")

    # the gate refuses the apply of known-broken syntax
    assert run(project, "--apply", script=script) == 1
    out, err = capsys.readouterr()
    assert f"pyedit: syntax: {target}:1:" in err, f"{name}: gate silent"
    assert "nothing was written" in err
    assert not (project / target).exists()

    # --force writes anyway, and the write stays revertible
    assert run(project, "--force", "--apply", script=script) == 0
    out, err = capsys.readouterr()
    assert (project / target).read_text() == broken
    assert "applying with syntax problems (--force)" in err
    assert "# pyedit undo" in out, f"{name}: forced write has no undo id"


def test_force_applies_broken_syntax_with_undo(project, capsys):
    broken = project / "bad.py"
    broken.write_text('pyedit.write("src/broken.py", "def (:\\n")\n')
    assert run(project, "--force", "--apply", script=broken) == 0
    out, err = capsys.readouterr()
    assert (project / "src" / "broken.py").read_text() == "def (:\n"
    assert "applying with syntax problems (--force)" in err
    assert "pyedit: syntax: src/broken.py:1:" in err  # still on record
    assert "# pyedit undo" in out  # the forced write is still revertible


def test_diff_flag_resolves_stored_id(project, script, capsys):
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    token = re.search(r"# pyedit dry-run ([0-9a-f]{8})", out).group(1)
    # re-rendering the stored diff chains a fresh dry-run with a fresh id
    assert run(project, "-d", token) == 0
    out = capsys.readouterr().out
    assert out.count("# pyedit dry-run") == 2
    assert re.search(r"# pyedit dry-run ([0-9a-f]{8})", out).group(1) != token


def test_commented_stdin_round_trips(project, script, capsys, monkeypatch):
    assert run(project, script=script) == 0
    out = capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO(out))
    assert run(project, "--apply") == 0
    assert (project / "src" / "a.py").read_text() == "ALPHA = 1\n"


def test_output_file_keeps_pure_diff(project, script, capsys):
    target = project / "changes.diff"
    assert run(project, "--output", target, script=script) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "# pyedit" not in target.read_text()
    assert re.search(r"saved as ([0-9a-f]{8})", err)


def test_apply_unknown_id_errors(project, capsys):
    with pytest.raises(SystemExit, match="no stored dry-run"):
        run(project, "--apply", "ffffffff")


def test_no_changes_stores_nothing(project, capsys, dryrun_store):
    quiet = project / "noop.py"
    quiet.write_text("pass\n")
    assert run(project, script=quiet) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "no changes" in err
    assert not dryrun_store.exists()


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


def test_stdin_script_mode(project, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('pyedit.edit("src/a.py", "alpha", "ALPHA")\n'),
    )
    assert run(project) == 0
    assert "+ALPHA = 1" in capsys.readouterr().out
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


def test_symlink_target_is_not_syntax_checked(project):
    script = project / "edit.py"
    script.write_text("pyedit.symlink('not python (', 'broken.py')\n")
    assert run(project, "--apply", script=script) == 0
    assert (project / "broken.py").is_symlink()


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


def test_file_budget_aborts_run(project, capsys):
    script = project / "edit.py"
    script.write_text(
        'for i in range(3):\n    pyedit.write(f"src/gen_{i}.py", "x\\n")\n'
    )
    before = set((project / "src").iterdir())
    assert run(project, "--max-materialized-files", "2", script=script) == 1
    _, err = capsys.readouterr()
    assert "file budget" in err
    assert "nothing was written" in err
    assert set((project / "src").iterdir()) == before


def test_file_budget_zero_disables(project, capsys):
    script = project / "edit.py"
    script.write_text(
        'for i in range(30):\n    pyedit.write(f"src/gen_{i}.py", "x\\n")\n'
    )
    assert run(project, "--max-materialized-files", "0", script=script) == 0
    out = capsys.readouterr().out
    assert out.count("+++ b/src/gen_") == 30


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
    assert "## Source" in out
    assert "github.com/lillecarl/pyedit" in out
    assert str(Path(pyedit.skill.__file__).resolve().parent) in out


def test_skill_writes_file(project, capsys):
    target = project / "docs" / "skill.md"
    assert cli.main(["skill", str(target)]) == 0
    assert capsys.readouterr().out == ""
    content = target.read_text()
    assert content.startswith("# pyedit")
    assert "## Source" in content
    assert str(Path(pyedit.skill.__file__).resolve().parent) in content
