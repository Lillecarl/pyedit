"""Edit the tree at a jj revision, amend it back.

Everything here runs against fakes: pyjj is an optional dependency
and no test may need the real package or a jj binary. The fakes mirror
the pyjj surface jjrev uses (open/resolve/read_file/is_executable/
list_files/atomic/squash/operation_id); a live -r run proves the
mirror.
"""

import os
import types
from pathlib import Path

import pytest

from pyedit import jjrev
from pyedit.jjrev import JjRevError
from pyedit.session import EditSession


class FakeId:
    def __init__(self, value):
        self.value = value

    def hex(self):
        return self.value


class FakeCommit:
    def __init__(self, files=None, execs=(), links=(), cid="aaaa"):
        self._files = dict(files or {})
        self._execs = set(execs)
        self._links = set(links)
        self._id = cid

    @property
    def id(self):
        return FakeId(self._id)

    def list_files(self, paths):
        names = sorted(self._files)
        if not paths:
            return names
        return [n for n in names if any(n == p or n.startswith(p + "/") for p in paths)]

    def read_file(self, path):
        if path not in self._files:
            raise FakeError(f"`{path}` does not exist")
        payload = self._files[path]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def is_executable(self, path):
        if path not in self._files or path in self._links:
            return None
        return path in self._execs


class FakeError(Exception):
    pass


class FakeTx:
    def __init__(self, repo):
        self.repo = repo
        self.squashed = []

    def squash(self, revision, into=None, paths=None):
        self.squashed.append((revision, into, list(paths or [])))
        if self.repo.checkout_bytes is not None:
            for rel in paths or []:
                (Path(self.repo.root) / rel).write_bytes(self.repo.checkout_bytes)
        return FakeCommit(cid="rewritten")


class FakeAtomic:
    def __init__(self, repo, description, allow_conflicts):
        self.repo = repo
        self.description = description
        self.allow_conflicts = allow_conflicts
        self.tx = None

    def __enter__(self):
        self.repo.atomics.append(self)
        self.tx = FakeTx(self.repo)
        return self.tx

    def __exit__(self, *exc):
        return False


class FakeRepo:
    def __init__(self, commits, root):
        self.commits = commits
        self._root = root
        self.atomics = []
        # bytes the fake squash lays into the working copy, standing
        # in for jj checking out rebased descendants
        self.checkout_bytes = None

    @property
    def root(self):
        return self._root

    def resolve(self, rev):
        try:
            return self.commits[rev]
        except KeyError:
            raise FakeError(f"revision {rev!r} names nothing") from None

    def atomic(self, description, allow_conflicts=False):
        return FakeAtomic(self, description, allow_conflicts)

    @property
    def operation_id(self):
        return "op999"


def fake_repo(root, monkeypatch):
    (root / "a.py").write_text("old\n")
    at = FakeCommit(files={"a.py": b"old\n"}, cid="at")
    target = FakeCommit(files={"a.py": b"older\n"}, cid="target")
    repo = FakeRepo({"@": at, "@-": target}, str(root))
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    return repo


class FakeMovingRepo(FakeRepo):
    """@ resolves differently across calls: the amend's rebase moved
    it mid-run, so reconcile meets a new working-copy commit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.at_versions = []
        self._at_calls = 0

    def resolve(self, rev):
        if rev == "@" and self.at_versions:
            idx = min(self._at_calls, len(self.at_versions) - 1)
            self._at_calls += 1
            return self.at_versions[idx]
        return super().resolve(rev)


def fake_moving_repo(root, monkeypatch, old_bytes, new_bytes):
    (root / "a.py").write_bytes(old_bytes)
    at_old = FakeCommit(files={"a.py": old_bytes}, cid="at-old")
    at_new = FakeCommit(files={"a.py": new_bytes}, cid="at-new")
    target = FakeCommit(files={"a.py": b"older\n"}, cid="target")
    repo = FakeMovingRepo({"@": at_old, "@-": target}, str(root))
    repo.at_versions = [at_old, at_new]
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    return repo


def test_export_materializes_bytes_modes_and_links(tmp_path, monkeypatch):
    target = FakeCommit(
        files={"a.py": b"x\n", "bin/run": b"run\n", "doc": b"elsewhere"},
        execs={"bin/run"},
        links={"doc"},
    )
    repo = FakeRepo({"@-": target}, str(tmp_path))
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    names = jjrev.export_tree(tmp_path, "@-", tmp_path / "out")
    assert names == ["a.py", "bin/run", "doc"]
    assert (tmp_path / "out" / "a.py").read_bytes() == b"x\n"
    assert (tmp_path / "out" / "bin" / "run").stat().st_mode & 0o111
    assert not ((tmp_path / "out" / "a.py").stat().st_mode & 0o111)
    assert os.readlink(tmp_path / "out" / "doc") == "elsewhere"


def test_export_unreadable_entry_is_loud(tmp_path, monkeypatch):
    target = FakeCommit(files={"sub": FakeError("is a Git submodule")})
    repo = FakeRepo({"@-": target}, str(tmp_path))
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    with pytest.raises(JjRevError, match="sub"):
        jjrev.export_tree(tmp_path, "@-", tmp_path / "out")


def test_amend_moves_changes_and_restores_wc(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    new_id, op = jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert (repo.atomics[0].allow_conflicts, new_id, op) == (True, "rewritten", "op999")
    assert repo.atomics[0].tx.squashed == [("@", "@-", ["a.py"])]
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_amend_dirty_working_copy_refuses(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    (tmp_path / "a.py").write_bytes(b"mine\n")
    with pytest.raises(JjRevError, match="uncommitted changes in a.py"):
        jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert repo.atomics == []
    assert (tmp_path / "a.py").read_bytes() == b"mine\n"


def test_amend_adds_and_deletes(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    (tmp_path / "gone.py").write_bytes(b"bye\n")
    repo.commits["@"] = FakeCommit(
        files={"a.py": b"old\n", "gone.py": b"bye\n"}, cid="at"
    )
    new_id, op = jjrev.amend_commit(
        tmp_path, "@-", {"fresh.py": b"hi\n", "gone.py": None}
    )
    assert op == "op999"
    assert repo.atomics[0].tx.squashed == [("@", "@-", ["fresh.py", "gone.py"])]
    assert not (tmp_path / "fresh.py").exists()
    assert (tmp_path / "gone.py").read_bytes() == b"bye\n"


def test_amend_without_pyjj(monkeypatch):
    monkeypatch.setattr(jjrev, "pyjj", None)
    with pytest.raises(JjRevError, match="runtime closure"):
        jjrev.find_repo_root(Path("/tmp"))


def test_revision_at_short_circuits_to_normal_flow(tmp_path, monkeypatch):
    from pyedit import runner

    monkeypatch.setattr(jjrev, "find_repo_root", lambda start: tmp_path)
    monkeypatch.setattr(jjrev, "resolve_ids", lambda root, rev: ("x", "x"))

    def no_amend(*args, **kwargs):
        raise AssertionError("amend called on the -r @ short-circuit")

    monkeypatch.setattr(jjrev, "amend_commit", no_amend)
    (tmp_path / "a.py").write_text("a\n")
    opts = runner.Options(root=tmp_path)

    def stage(session):
        session.write("a.py", "b\n")

    result = runner.run_revision(opts, "@", stage)
    assert result.applied is False and result.op is None
    assert result.files == [str(tmp_path / "a.py")]


def test_revision_flow_exports_and_amends(tmp_path, monkeypatch):
    from pyedit import runner

    monkeypatch.setattr(jjrev, "find_repo_root", lambda start: tmp_path)
    monkeypatch.setattr(jjrev, "resolve_ids", lambda root, rev: ("aaa", "bbb"))

    exported = {}

    def fake_export(repo_root, rev, dest):
        exported["dest"] = dest
        (dest / "a.py").write_bytes(b"old\n")
        return ["a.py"]

    monkeypatch.setattr(jjrev, "export_tree", fake_export)
    amended = {}

    def fake_amend(repo_root, rev, changes):
        amended.update(changes)
        return ("newid", "op1")

    monkeypatch.setattr(jjrev, "amend_commit", fake_amend)
    (tmp_path / "a.py").write_text("old\n")
    opts = runner.Options(root=tmp_path, apply=True)

    def stage(session):
        assert session.root != tmp_path
        session.write("a.py", "new\n")

    result = runner.run_revision(opts, "@-", stage)
    assert amended == {"a.py": "new\n"}
    assert result.applied is True and result.op == "op1"
    assert result.files == ["a.py"]


def test_cli_parses_revision():
    from pyedit.cli import build_parser

    assert build_parser().parse_args(["-r", "abc@", "-s", "x"]).revision == "abc@"
    assert build_parser().parse_args(["-s", "x"]).revision is None


def test_mcp_revision_needs_pyjj(tmp_path, monkeypatch):
    monkeypatch.setattr(jjrev, "pyjj", None)
    from pyedit.mcp_server import run_edit

    out = run_edit(
        script='pyedit.write("a.py", "x\n")',
        workdir=str(tmp_path),
        revision="@-",
    )
    assert out["ok"] is False and "runtime closure" in out["error"]


def test_markers_resolve_in_place(tmp_path):
    body = (
        "<<<<<<< conflict 1 of 1\n"
        "+++++++ merged\n"
        "def f():\n"
        "%%%%%%% diff from: base\n"
        "-    return 1\n"
        "+    return 2\n"
        "+++++++ merged\n"
        "    return 2\n"
        ">>>>>>> conflict 1 of 1 ends\n"
    )
    (tmp_path / "m.py").write_text(body)
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write("m.py", "def f():\n    return 2\n")
    assert session.read("m.py") == "def f():\n    return 2\n"
    assert session.staged() != {}


def test_rebased_checkout_is_left_alone(tmp_path, monkeypatch):
    # the squash moved @ and jj checked the new tree out: restoring
    # our saved bytes here would dirty the copy, so reconcile leaves
    # matching files exactly as checkout wrote them
    repo = fake_moving_repo(tmp_path, monkeypatch, b"old\n", b"new\n")
    repo.checkout_bytes = b"new\n"
    new_id, op = jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert op == "op999"
    assert (tmp_path / "a.py").read_bytes() == b"new\n"


def test_rebased_mismatch_is_loud(tmp_path, monkeypatch):
    # something else wrote the copy in the window: the amend stands,
    # and the mismatch names the op that holds it
    repo = fake_moving_repo(tmp_path, monkeypatch, b"old\n", b"new\n")
    repo.checkout_bytes = b"someone-else\n"
    with pytest.raises(JjRevError, match="does not match rebased @"):
        jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert (tmp_path / "a.py").read_bytes() == b"someone-else\n"
