"""The configured [lsp.*] pass: real `ruff server` over staged files.

Staged Python gets fixAll, organizeImports and formatting; excluded
trees, wrong suffixes and unstaged disk files are left alone. No
matching file means the server never starts, so a bogus command
fails only when something actually matches.
"""

import shutil
from pathlib import Path

import pytest

from pyedit import cli, lsppass
from pyedit.config import Config, LspTable
from pyedit.session import EditSession


_ruff = shutil.which("ruff")
requires_ruff = pytest.mark.skipif(_ruff is None, reason="ruff is not on PATH")

RUFF = LspTable(
    command=["ruff", "server"],
    suffixes=["py"],
    actions=["source.fixAll.ruff", "source.organizeImports.ruff"],
    format=True,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompt-toolkit").mkdir()
    (tmp_path / "prompt-toolkit" / "vendored.py").write_text(
        "import os\nimport sys\nx=1\nprint(sys.argv,x)\n"
    )
    (tmp_path / "fix.py").write_text(
        "import os\nimport sys\nx=1\nprint(sys.argv,x)\n"
    )
    (tmp_path / "clean.py").write_text("import sys\n\nprint(sys.argv)\n")
    (tmp_path / "note.txt").write_text("hello\n")
    return EditSession(respect_gitignore=False, root=tmp_path)


def stage_all(session: EditSession) -> dict:
    for name in ("fix.py", "clean.py", "note.txt", "prompt-toolkit/vendored.py"):
        session.write(name, session.read(name))
    return session.staged()


@requires_ruff
def test_pass_fixes_formats_and_reports(tree):
    conf = Config(lsp={"ruff": RUFF})
    touched = lsppass.apply(tree, stage_all(tree), conf)
    assert touched == ["fix.py (ruff)", "prompt-toolkit/vendored.py (ruff)"]
    assert tree.read("fix.py") == "import sys\n\nx = 1\nprint(sys.argv, x)\n"
    assert tree.read("prompt-toolkit/vendored.py") == (
        "import sys\n\nx = 1\nprint(sys.argv, x)\n"
    )
    assert tree.read("clean.py") == "import sys\n\nprint(sys.argv)\n"
    assert tree.read("note.txt") == "hello\n"


@requires_ruff
def test_pass_skips_excluded(tree):
    conf = Config(lsp={"ruff": RUFF}, exclude=["prompt-toolkit/**"])
    before = (tree.root / "prompt-toolkit" / "vendored.py").read_text()
    touched = lsppass.apply(tree, stage_all(tree), conf)
    assert touched == ["fix.py (ruff)"]
    assert tree.read("prompt-toolkit/vendored.py") == before
    assert tree.read("fix.py") == "import sys\n\nx = 1\nprint(sys.argv, x)\n"


def test_no_match_never_starts_the_server(tree):
    conf = Config(
        lsp={
            "bogus": LspTable(
                command=["definitely-not-a-server"],
                suffixes=["py"],
                actions=["source.fixAll.ruff"],
            )
        }
    )
    session_files = {
        path: content
        for path, content in stage_all(tree).items()
        if Path(path).suffix != ".py"
    }
    assert lsppass.apply(tree, session_files, conf) == []


@requires_ruff
def test_missing_binary_is_loud(tree):
    conf = Config(
        lsp={
            "bogus": LspTable(
                command=["definitely-not-a-server"],
                suffixes=["py"],
                actions=["source.fixAll.ruff"],
            )
        }
    )
    with pytest.raises(lsppass.LspPassError, match="lsp.bogus"):
        lsppass.apply(tree, stage_all(tree), conf)
    assert tree.read("fix.py") == "import os\nimport sys\nx=1\nprint(sys.argv,x)\n"


@requires_ruff
def test_cli_run_applies_lsp_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    before = "import os\nimport sys\nx=1\nprint(sys.argv,x)\n"
    (tmp_path / "a.py").write_text(before)
    (tmp_path / "pyedit.toml").write_text(
        '[lsp.ruff]\ncommand = ["ruff", "server"]\nsuffixes = ["py"]\n'
        'actions = ["source.fixAll.ruff"]\nformat = true\n'
    )
    script = tmp_path / "edit.py"
    script.write_text('pyedit.edit("a.py", "x=1", "x = 1")\n')
    assert cli.main(["--script", str(script)]) == 0
    out, err = capsys.readouterr()
    assert "-import os" in out
    assert "print(sys.argv, x)" in out
    assert "pyedit: actions: a.py (ruff)" in err
    assert (tmp_path / "a.py").read_text() == before


@requires_ruff
def test_format_only_table(tree):
    conf = Config(
        lsp={
            "ruff": LspTable(
                command=["ruff", "server"], suffixes=["py"], format=True
            )
        }
    )
    (tree.root / "messy.py").write_text("x=1\n")
    tree.write("messy.py", tree.read("messy.py"))
    assert lsppass.apply(tree, tree.staged(), conf) == ["messy.py (ruff)"]
    assert tree.read("messy.py") == "x = 1\n"
