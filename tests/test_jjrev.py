"""Edit the tree at a jj revision, amend it back.

Everything here runs against fakes: pyjj is an optional dependency
and no test may need the real package or a jj binary. The fakes mirror
the pyjj surface jjrev uses (open/resolve/read_file/is_executable/
list_files/atomic/restore/describe/conflicts/operation_id/settings,
the author rewrite through tx.transaction, Signature/Timestamp
constructors, plus the entry snapshot atomic() takes and the checkout
it writes on exit); restore copies exact bytes the way jj restore
does, never a merge. Rebase itself is jj's half and live-covered;
the fakes pin our half: exact bytes into the target, metadata in the
same block, the working copy put back, conflicts reported.
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
    def __init__(
        self,
        files=None,
        execs=(),
        links=(),
        cid="aaaa",
        description="",
        author=None,
        parents=(),
        conflicted=False,
    ):
        self._files = dict(files or {})
        self._execs = set(execs)
        self._links = set(links)
        self._id = cid
        self.description = description
        self.author = author or FakeSignature("A U Thor", "author@example.com")
        self._parents = list(parents)
        self.has_conflict = conflicted

    @property
    def id(self):
        return FakeId(self._id)

    @property
    def parent_ids(self):
        return list(self._parents)

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


class FakeTimestamp:
    def __init__(self, millis=0, tz=0):
        self.millis_since_epoch = millis
        self.tz_offset_minutes = tz


class FakeSignature:
    def __init__(self, name, email, timestamp=None):
        self.name = name
        self.email = email
        self.timestamp = timestamp or FakeTimestamp()


def fake_module(repo):
    """A pyjj stand-in with the constructors jjrev reaches for."""
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    module.Signature = FakeSignature
    module.Timestamp = FakeTimestamp
    return module


class FakeBuilder:
    def __init__(self, commit):
        self.commit = commit

    def set_description(self, message):
        self.commit.description = message
        return self

    def set_author(self, sig):
        self.commit.author = sig
        return self

    def set_parents(self, parents):
        self.commit._parents = list(parents)
        return self

    def write(self, _repo):
        return self.commit


class FakeRawTx:
    """The escape hatch jjrev rewrites the author through: the
    public builder, since the wrapper has no author verb."""

    def __init__(self, repo):
        self.repo = repo
        self.rewrites = []

    def rewrite_commit(self, _settings, commit):
        self.rewrites.append(commit.id.hex())
        return FakeBuilder(commit)

    def check_rewritable(self, _settings, ids):
        self.repo.checked.extend(commit_id.hex() for commit_id in ids)
        blocked = self.repo.unrewritable & {commit_id.hex() for commit_id in ids}
        if blocked:
            raise FakeError(f"mirror: {sorted(blocked)} are immutable")


class FakeTx:
    def __init__(self, repo):
        self.repo = repo
        self.restored = []
        self.described = []
        self.news = []
        self.raw = FakeRawTx(repo)

    @property
    def transaction(self):
        return self.raw

    def describe(self, revision, message):
        target = (
            revision if not isinstance(revision, str) else self.repo.resolve(revision)
        )
        self.described.append((target.id.hex(), message))
        target.description = message
        return target

    def new(self, parents, message=None, edit=True):
        """An empty child commit, detached: the block's own restores
        take its object directly, never a re-resolution."""
        if isinstance(parents, str):
            parents = [parents]
        self.news.append((list(parents), message, edit))
        child = FakeCommit(cid=f"new-{len(self.news)}", description=message or "")
        # reachable the way a post-block resolution finds it
        self.repo.by_cid[child.id.hex()] = child
        return child

    def restore(self, paths, into="@", from_revision=None):
        if from_revision is None:
            raise FakeError("mirror: restore needs from_revision")
        # None paths carry the whole source tree, the way the
        # binding restores everything when paths is None; a commit
        # object into records under its own id
        recorded = into if isinstance(into, str) else into.id.hex()
        self.restored.append(
            (list(paths) if paths is not None else None, recorded, from_revision)
        )
        self.repo.last_paths = list(paths) if paths is not None else None
        source = self.repo.resolve(from_revision)
        target = into if not isinstance(into, str) else self.repo.resolve(into)
        names = paths if paths is not None else list(source._files)
        for rel in names:
            if rel in source._files:
                target._files[rel] = source._files[rel]
                if rel in source._execs:
                    target._execs.add(rel)
                else:
                    target._execs.discard(rel)
                if rel in source._links:
                    target._links.add(rel)
                else:
                    target._links.discard(rel)
            else:
                target._files.pop(rel, None)
                target._execs.discard(rel)
                target._links.discard(rel)
        if recorded != "@":
            # the block rewrote history: jj rebases @ onto it, so
            # later @ reads meet the rebased commit
            self.repo.moved = True
        # the rewritten commit itself: later rewrites in the same
        # block (describe, author) land on it, not on an orphan
        return target


class FakeAtomic:
    def __init__(self, repo, description, allow_conflicts):
        self.repo = repo
        self.description = description
        self.allow_conflicts = allow_conflicts
        self.tx = None

    def __enter__(self):
        self.repo.atomics.append(self)
        self.repo.snapshot()
        self.tx = FakeTx(self.repo)
        return self.tx

    def __exit__(self, *exc):
        if self.repo.checkout_bytes is not None:
            for rel in self.repo.last_paths or []:
                (Path(self.repo.root) / rel).write_bytes(self.repo.checkout_bytes)
        return False


class FakeRepo:
    def __init__(self, commits, root):
        self.commits = commits
        self._root = root
        self.atomics = []
        self.by_cid = {}
        self.moved = False
        self.at_new = None
        # commits carrying markers, for the conflicts report
        self.conflicted = []
        # bytes the fake checkout lays into the working copy on
        # atomic exit, standing in for jj writing out the moved @
        self.checkout_bytes = None
        self.last_paths = None
        # sentinels for the escape hatch (settings, readonly repo)
        self.settings = object()
        self._repo = object()
        # ids the guard was asked about; hexes it must refuse
        self.checked = []
        self.unrewritable = set()

    @property
    def root(self):
        return self._root

    def resolve(self, rev):
        if rev == "@" and self.moved and self.at_new is not None:
            return self.at_new
        try:
            return self.commits[rev]
        except KeyError:
            pass
        for commit in list(self.commits.values()) + list(self.by_cid.values()):
            if commit.id.hex() == rev:
                return commit
        raise FakeError(f"revision {rev!r} names nothing")

    def revset(self, rev):
        # children queries only ("HEX+"): the commits naming that
        # hex among their parents, in repo order
        if not rev.endswith("+"):
            raise FakeError(f"mirror: revset models children queries, got {rev!r}")
        base = rev[:-1]
        return [
            commit
            for commit in self.commits.values()
            if base in [parent.hex() for parent in commit.parent_ids]
        ]

    def snapshot(self):
        """Fold the working-copy files into @, the way atomic entry
        does; the replaced commit stays reachable by its id. A clean
        working copy rewrites nothing, the way the binding skips a
        snapshot that would bump the id for no reason."""
        old = self.commits["@"]
        files = {}
        for path in Path(self._root).rglob("*"):
            if path.is_file() and not path.is_symlink():
                files[path.relative_to(self._root).as_posix()] = path.read_bytes()
        if files == old._files:
            return
        self.by_cid[old.id.hex()] = old
        self.commits["@"] = FakeCommit(
            files=files,
            cid=old.id.hex() + "-snap",
            description=old.description,
            author=old.author,
            parents=old.parent_ids,
        )

    def conflicts(self):
        return list(self.conflicted)

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
    monkeypatch.setattr(jjrev, "pyjj", fake_module(repo))
    return repo


def fake_moving_repo(root, monkeypatch, old_bytes, rebased_bytes):
    """@ carries rebased content after the amend: the rebase moved it
    mid-run, so reconcile meets a new working-copy commit."""
    (root / "a.py").write_bytes(old_bytes)
    at_old = FakeCommit(files={"a.py": old_bytes}, cid="at-old")
    at_new = FakeCommit(files={"a.py": rebased_bytes}, cid="at-new")
    target = FakeCommit(files={"a.py": b"older\n"}, cid="target")
    repo = FakeRepo({"@": at_old, "@-": target}, str(root))
    repo.at_new = at_new
    monkeypatch.setattr(jjrev, "pyjj", fake_module(repo))
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
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {"a.py": b"new\n"}
    )
    assert (new_id, op, cleanup, conflicts) == ("target", "op999", "op999", [])
    assert repo.atomics[0].allow_conflicts is True
    assert repo.atomics[0].tx.restored == [(["a.py"], "@-", "@")]
    assert repo.atomics[1].tx.restored == [(["a.py"], "@", "at")]
    assert repo.resolve("@-").read_file("a.py") == b"new\n"
    assert repo.resolve("@").read_file("a.py") == b"old\n"
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
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {"fresh.py": b"hi\n", "gone.py": None}
    )
    assert (op, cleanup, conflicts) == ("op999", "op999", [])
    assert repo.atomics[0].tx.restored == [(["fresh.py", "gone.py"], "@-", "@")]
    target = repo.resolve("@-")
    assert target.read_file("fresh.py") == b"hi\n"
    with pytest.raises(FakeError):
        target.read_file("gone.py")
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
    monkeypatch.setattr(jjrev, "target_meta", lambda root, rev: {})

    exported = {}

    def fake_export(repo_root, rev, dest):
        exported["dest"] = dest
        (dest / "a.py").write_bytes(b"old\n")
        return ["a.py"]

    monkeypatch.setattr(jjrev, "export_tree", fake_export)
    amended = {}

    def fake_amend(repo_root, rev, changes, meta=None):
        amended.update(changes)
        amended["meta"] = meta
        return ("newid", "op1", None, [])

    monkeypatch.setattr(jjrev, "amend_commit", fake_amend)
    (tmp_path / "a.py").write_text("old\n")
    opts = runner.Options(root=tmp_path, apply=True)

    def stage(session):
        assert session.root != tmp_path
        session.write("a.py", "new\n")

    result = runner.run_revision(opts, "@-", stage)
    assert amended == {"a.py": "new\n", "meta": None}
    assert result.applied is True and result.op == "op1"
    assert result.cleanup_op is None and result.conflicts == []
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
    # the rebase moved @ onto content that is neither the pre-run
    # state nor the staged bytes: checkout wrote it out, so reconcile
    # verifies and leaves files exactly as checkout wrote them, with
    # no cleanup transaction
    repo = fake_moving_repo(tmp_path, monkeypatch, b"old\n", b"rebased\n")
    repo.checkout_bytes = b"rebased\n"
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {"a.py": b"new\n"}
    )
    assert (op, cleanup, conflicts) == ("op999", None, [])
    assert len(repo.atomics) == 1
    assert (tmp_path / "a.py").read_bytes() == b"rebased\n"


def test_rebased_mismatch_is_loud(tmp_path, monkeypatch):
    # something else wrote the copy in the window: the amend stands,
    # and the mismatch names the op that holds it
    repo = fake_moving_repo(tmp_path, monkeypatch, b"old\n", b"rebased\n")
    repo.checkout_bytes = b"someone-else\n"
    with pytest.raises(JjRevError, match="does not match rebased @"):
        jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert (tmp_path / "a.py").read_bytes() == b"someone-else\n"


def test_divergent_tip_content_stays_out_of_target(tmp_path, monkeypatch):
    # the tip changed the same file the amend rewrites: the target
    # takes the staged bytes exactly (restore copies, a squash would
    # merge), and @ goes back to the tip content it held
    (tmp_path / "a.py").write_bytes(b"tip\n")
    at = FakeCommit(files={"a.py": b"tip\n"}, cid="at")
    target = FakeCommit(files={"a.py": b"base\n"}, cid="target")
    repo = FakeRepo({"@": at, "@-": target}, str(tmp_path))
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {"a.py": b"mine\n"}
    )
    assert (op, cleanup, conflicts) == ("op999", "op999", [])
    assert len(repo.atomics) == 2
    assert repo.resolve("@-").read_file("a.py") == b"mine\n"
    assert repo.resolve("@").read_file("a.py") == b"tip\n"
    assert (tmp_path / "a.py").read_bytes() == b"tip\n"


def test_conflicts_are_reported(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    repo.conflicted = [FakeCommit(cid="cccc", description="C: line\nbody")]
    _, _, _, conflicts = jjrev.amend_commit(tmp_path, "@-", {"a.py": b"new\n"})
    assert conflicts == ["cccc C: line"]


def test_rev_naming_wc_refuses(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_bytes(b"old\n")
    at = FakeCommit(files={"a.py": b"old\n"}, cid="at")
    repo = FakeRepo({"@": at}, str(tmp_path))
    module = types.ModuleType("pyjj")
    module.open = lambda path: repo
    monkeypatch.setattr(jjrev, "pyjj", module)
    with pytest.raises(JjRevError, match="working copy itself"):
        jjrev.amend_commit(tmp_path, "@", {"a.py": b"new\n"})
    assert repo.atomics == []
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_amend_applies_tree_and_meta_in_one_block(tmp_path, monkeypatch):
    # description and author land in the same transaction as the
    # tree restore: one op holds the whole amend (the second atomic
    # is the pre-existing working-copy cleanup, not a second amend)
    repo = fake_repo(tmp_path, monkeypatch)
    target = repo.resolve("@-")
    target.description = "old subject"
    meta = {"description": "new subject", "author": "New Name <new@example.com>"}
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {"a.py": b"new\n"}, meta
    )
    assert (op, cleanup, conflicts) == ("op999", "op999", [])
    assert len(repo.atomics) == 2
    assert repo.atomics[0].tx.restored == [(["a.py"], "@-", "@")]
    assert repo.atomics[0].tx.described == [("target", "new subject")]
    assert repo.atomics[0].tx.raw.rewrites == ["target"]
    assert repo.resolve("@-").read_file("a.py") == b"new\n"
    assert repo.resolve("@-").description == "new subject"
    author = repo.resolve("@-").author
    assert (author.name, author.email) == ("New Name", "new@example.com")
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_amend_meta_only_skips_the_tree_restore(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    new_id, op, cleanup, conflicts = jjrev.amend_commit(
        tmp_path, "@-", {}, {"description": "reworded"}
    )
    assert (op, cleanup, conflicts) == ("op999", None, [])
    assert len(repo.atomics) == 1
    assert repo.atomics[0].tx.restored == []
    assert repo.resolve("@-").description == "reworded"
    assert repo.resolve("@-").read_file("a.py") == b"older\n"


def test_amend_empty_is_loud(tmp_path, monkeypatch):
    fake_repo(tmp_path, monkeypatch)
    with pytest.raises(JjRevError, match="nothing to amend"):
        jjrev.amend_commit(tmp_path, "@-", {}, None)
    with pytest.raises(JjRevError, match="nothing to amend"):
        jjrev.amend_commit(tmp_path, "@-", {}, {})


def test_retitle_sets_metadata_without_tree_changes(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    op = jjrev.retitle(
        tmp_path,
        "@",
        {"description": "wc subject", "author": "W C <wc@example.com>"},
    )
    assert op == "op999"
    assert len(repo.atomics) == 1
    assert repo.resolve("@").description == "wc subject"
    assert repo.resolve("@").author.email == "wc@example.com"
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_target_meta_reads_old_values(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    repo.resolve("@-").description = "base subject"
    assert jjrev.target_meta(tmp_path, "@-") == {
        "description": "base subject",
        "author": "A U Thor <author@example.com>",
    }


def test_run_revision_applies_and_prunes_meta(tmp_path, monkeypatch):
    # a reword to the same message rewrites nothing: the amend gets
    # only what changed
    from pyedit import runner

    monkeypatch.setattr(jjrev, "find_repo_root", lambda start: tmp_path)
    monkeypatch.setattr(jjrev, "resolve_ids", lambda root, rev: ("aaa", "bbb"))
    monkeypatch.setattr(
        jjrev,
        "target_meta",
        lambda root, rev: {
            "description": "same",
            "author": "A U Thor <author@example.com>",
        },
    )

    def fake_export(repo_root, rev, dest):
        (dest / "a.py").write_bytes(b"old\n")
        return ["a.py"]

    monkeypatch.setattr(jjrev, "export_tree", fake_export)
    amended = {}

    def fake_amend(repo_root, rev, changes, meta=None):
        amended["changes"] = changes
        amended["meta"] = meta
        return ("newid", "op1", None, [])

    monkeypatch.setattr(jjrev, "amend_commit", fake_amend)
    (tmp_path / "a.py").write_text("old\n")
    opts = runner.Options(root=tmp_path, apply=True)

    def stage(session):
        session.write("a.py", "new\n")
        session.describe("same")
        session.author("New Name <new@example.com>")

    result = runner.run_revision(opts, "@-", stage)
    assert amended["meta"] == {"author": "New Name <new@example.com>"}
    assert result.applied is True and result.op == "op1"
    assert result.meta == {
        "author": ("A U Thor <author@example.com>", "New Name <new@example.com>")
    }


def test_run_revision_dry_run_shows_meta_and_writes_nothing(tmp_path, monkeypatch):
    from pyedit import runner

    monkeypatch.setattr(jjrev, "find_repo_root", lambda start: tmp_path)
    monkeypatch.setattr(jjrev, "resolve_ids", lambda root, rev: ("aaa", "bbb"))
    monkeypatch.setattr(
        jjrev,
        "target_meta",
        lambda root, rev: {"description": "old", "author": "A <a@x>"},
    )

    def fake_export(repo_root, rev, dest):
        (dest / "a.py").write_bytes(b"old\n")
        return ["a.py"]

    monkeypatch.setattr(jjrev, "export_tree", fake_export)

    def no_amend(*args, **kwargs):
        raise AssertionError("amend called on a dry-run")

    monkeypatch.setattr(jjrev, "amend_commit", no_amend)
    (tmp_path / "a.py").write_text("old\n")
    opts = runner.Options(root=tmp_path)

    def stage(session):
        session.describe("new")

    result = runner.run_revision(opts, "@-", stage)
    assert result.applied is False and result.op is None
    assert result.meta == {"description": ("old", "new")}
    assert result.files == []


# A below B below @, plus a merge M off B: the insert reparenting shape
def _commit_stack(root, monkeypatch):
    (root / "a.py").write_bytes(b"b1\n")
    (root / "keep.py").write_bytes(b"keep\n")
    (root / "b.py").write_bytes(b"b\n")
    a = FakeCommit(files={"a.py": b"a1\n", "keep.py": b"keep\n"}, cid="aaaa")
    b = FakeCommit(
        files={"a.py": b"b1\n", "keep.py": b"keep\n", "b.py": b"b\n"},
        cid="bbbb",
        parents=[FakeId("aaaa")],
        description="B subject",
    )
    at = FakeCommit(
        files={"a.py": b"b1\n", "keep.py": b"keep\n", "b.py": b"b\n"},
        cid="atat",
        parents=[FakeId("bbbb")],
    )
    m = FakeCommit(
        files={"a.py": b"b1\n", "m.py": b"m\n"},
        cid="mmmm",
        parents=[FakeId("bbbb"), FakeId("aaaa")],
    )
    repo = FakeRepo({"@": at, "B": b, "A": a, "M": m}, str(root))
    monkeypatch.setattr(jjrev, "pyjj", fake_module(repo))
    return repo


def test_create_after_inserts_with_full_tree_and_reparents(tmp_path, monkeypatch):
    # the new commit holds REV's whole tree with the staged values
    # over it, and every child of REV moves onto it keeping its
    # other parents: merges stay merges
    repo = _commit_stack(tmp_path, monkeypatch)
    new_hex, op, cleanup, conflicts = jjrev.create_after(
        tmp_path, "B", "inserted", {"a.py": b"a2\n"}
    )
    assert new_hex == "new-1"
    # no cleanup transaction: @ was reparented, so its staged bytes
    # arrived through the rebase, not as leftover vehicle, and the
    # files stay as checkout wrote them
    assert (op, cleanup, conflicts) == ("op999", None, [])
    assert len(repo.atomics) == 1
    tx = repo.atomics[0].tx
    assert tx.news == [(["B"], "inserted", False)]
    assert tx.restored[0] == (None, "new-1", "B")
    assert tx.restored[1] == (["a.py"], "new-1", "@")
    # post-snapshot ids: the explicit snapshot folded the vehicle
    # into @ before anything resolved the block's inputs
    assert repo.checked == ["atat-snap", "mmmm"]
    assert tx.raw.rewrites == ["atat-snap", "mmmm"]
    inserted = repo.resolve(new_hex)
    assert inserted.description == "inserted"
    assert inserted.read_file("a.py") == b"a2\n"
    assert inserted.read_file("keep.py") == b"keep\n"
    assert inserted.read_file("b.py") == b"b\n"
    assert [p.hex() for p in repo.resolve("@").parent_ids] == ["new-1"]
    assert [p.hex() for p in repo.resolve("M").parent_ids] == ["new-1", "aaaa"]
    assert repo.resolve("@").read_file("a.py") == b"a2\n"
    assert repo.resolve("B").read_file("a.py") == b"b1\n"
    assert (tmp_path / "a.py").read_bytes() == b"a2\n"


def test_create_after_without_children_skips_surgery(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    new_hex, op, cleanup, conflicts = jjrev.create_after(
        tmp_path, "@-", "inserted", {"a.py": b"x\n"}
    )
    assert (op, cleanup, conflicts) == ("op999", "op999", [])
    assert repo.atomics[0].tx.raw.rewrites == []
    assert repo.checked == []
    assert repo.resolve(new_hex).read_file("a.py") == b"x\n"
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_create_after_working_copy_itself_refuses(tmp_path, monkeypatch):
    # whichever spelling names @, the half-snapshotted vehicle has
    # nothing sound to build on: describe it and advance instead
    repo = fake_repo(tmp_path, monkeypatch)
    with pytest.raises(JjRevError, match="working-copy commit itself"):
        jjrev.create_after(tmp_path, "@", "msg", {"a.py": b"x\n"})
    with pytest.raises(JjRevError, match="working-copy commit itself"):
        jjrev.create_after(tmp_path, "at", "msg", {"a.py": b"x\n"})
    assert repo.atomics == []
    assert (tmp_path / "a.py").read_bytes() == b"old\n"


def test_create_after_empty_scope_refuses(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    with pytest.raises(JjRevError, match="nothing to commit"):
        jjrev.create_after(tmp_path, "@-", "msg", {})
    assert repo.atomics == []


def test_create_after_conflicted_base_refuses(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    repo.resolve("@-").has_conflict = True
    with pytest.raises(JjRevError, match="carries conflicts"):
        jjrev.create_after(tmp_path, "@-", "msg", {"a.py": b"x\n"})
    assert repo.atomics == []


def test_create_after_immutable_child_refuses(tmp_path, monkeypatch):
    repo = _commit_stack(tmp_path, monkeypatch)
    repo.unrewritable = {"mmmm"}
    with pytest.raises(JjRevError, match="committing after"):
        jjrev.create_after(tmp_path, "B", "msg", {"a.py": b"a2\n"})


def test_create_after_message_needs_str(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path, monkeypatch)
    with pytest.raises(JjRevError, match="needs str"):
        jjrev.create_after(tmp_path, "@-", None, {"a.py": b"x\n"})
    assert repo.atomics == []
