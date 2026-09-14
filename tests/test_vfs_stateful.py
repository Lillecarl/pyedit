"""Model-checked interactions, driven by Hypothesis, two machines.

The root machine has no model at all: every operation runs twice,
once on the staged session and once on a real filesystem, and the
results must be equal -- reads raise or agree per path, and after
apply the whole staged tree equals the tree an ordinary script would
have produced. The real FS is the oracle; the staged layer is only
allowed to differ from it in *when* bytes land, never *what* lands.

The scope machine covers what the oracle cannot see: VFS scopes
merging into their parent. Its rules partition regions (the parent
edits a file's first token, scopes the last) so merge outcomes stay
predictable without reimplementing the merge. Every seed file keeps an
unchanged line between those two regions, because git merges two
changes only when one separates them.
"""

import binascii
import builtins
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, initialize, rule

import pyedit
from pyedit import vfs as vfs_module
from pyedit.merge import Collision, VFS
from pyedit.session import EditSession, Symlink

FILES = ["a.txt", "b.txt", "dir/c.txt"]
BIN = "img.bin"
SEED = {
    "a.txt": "A0\nmid\nA1\n",
    "b.txt": "B0\nmid\nB1\n",
    "dir/c.txt": "C0\nC1\nC2\n",
}
BIN_SEED = {BIN: b"\x00\x01\x02\n"}
FIRST = {"a.txt": "A0", "b.txt": "B0", "dir/c.txt": "C0"}
LAST = {"a.txt": "A1", "b.txt": "B1", "dir/c.txt": "C2"}
MOVE_TARGETS = ["moved.txt", "other.txt"]

texts = st.sampled_from(["new\n", "more\n", "", "two lines\n"])
parent_fragments = st.sampled_from(["A0", "B0", "C0", "zz"])
replacements = st.sampled_from(["X", "XY\n", ""])
scope_replacements = st.sampled_from(["S", "SS\n"])


class SessionAgainstRealDisk(RuleBasedStateMachine):
    """The staged layer against the filesystem as the oracle.

    Every rule mirrors the operation onto a real directory with
    ordinary filesystem calls; nothing is tracked except the two
    trees. The oracle is authoritative: its tree is what the staged
    view must show, and after apply the staged tree must equal it.
    """

    @initialize()
    def seed(self):
        self.a_root = Path(tempfile.mkdtemp())
        self.b_root = Path(tempfile.mkdtemp())
        for tree in (self.a_root, self.b_root):
            for rel, text in SEED.items():
                path = tree / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            for rel, blob in BIN_SEED.items():
                (tree / rel).write_bytes(blob)
        # capture the raw functions BEFORE the patch: the oracle side
        # must do real filesystem work while the overlay is installed
        self._raw = {
            "open": builtins.open,
            "rename": os.rename,
            "unlink": os.unlink,
            "rmtree": shutil.rmtree,
            "walk": os.walk,
            "isfile": os.path.isfile,
            "mkdir": os.mkdir,
            "isdir": os.path.isdir,
            "symlink": os.symlink,
            "readlink": os.readlink,
            "islink": os.path.islink,
        }
        self.session = EditSession(root=self.a_root)
        self.restore = vfs_module.install(self.session)

    def raw_makedirs(self, path):
        # os.makedirs re-looks-up os.mkdir at call time, which the
        # patch has replaced: build the tree from raw mkdir
        if self._raw["isdir"](path):
            return
        self.raw_makedirs(path.parent)
        try:
            self._raw["mkdir"](path)
        except FileExistsError:
            pass

    def raw_copyfile(self, src, dst):
        with self._raw["open"](src, "rb") as fh:
            data = fh.read()
        self.raw_makedirs(Path(dst).parent)
        with self._raw["open"](dst, "wb") as fh:
            fh.write(data)

    def oracle_read(self, rel):
        # bytes everywhere; OSError of any kind reads as absent, which
        # covers missing files, deleted-in-session paths and directories
        try:
            with self._raw["open"](self.b_root / rel, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def every_rel(self):
        rels = set(FILES) | set(MOVE_TARGETS) | {BIN}
        for path in self.session.staged():
            try:
                rels.add(str(path.relative_to(self.a_root)))
            except ValueError:
                # the patch stages everything the process writes,
                # the test framework's bookkeeping included
                continue
        for base, _dirs, names in self._raw["walk"](self.b_root):
            for name in names:
                rels.add(str(Path(base).relative_to(self.b_root) / name))
        return sorted(rels)

    def view_equals_oracle(self):
        # the oracle tree is the view: the view-equality invariant
        # makes a path's existence agree on both sides, so reads
        # raise or agree per path
        staged = self.session.staged()
        staged_names = {str(p.relative_to(self.a_root)) for p in staged}
        for rel in self.every_rel():
            if isinstance(staged.get(self.session.canon(rel)), Symlink):
                # a staged link cannot be followed; the apply
                # invariant compares the materialized trees
                continue
            if rel not in staged_names and self._raw["islink"](
                self.a_root / rel
            ):
                # an unstaged link chases A's real disk, while the
                # B mirror already holds staged effects on the
                # target; the trees agree only once applied
                continue
            oracle = self.oracle_read(rel)
            try:
                content = self.session.read(rel)
            except OSError:
                content = None
            if oracle is None:
                assert content is None
            elif isinstance(content, str):
                assert content.encode() == oracle
            else:
                assert content == oracle

    @rule(path=st.sampled_from(FILES), text=texts)
    def write(self, path, text):
        self.session.write(path, text)
        self.raw_makedirs((self.b_root / path).parent)
        with self._raw["open"](self.b_root / path, "w") as fh:
            fh.write(text)

    @rule(data=st.data())
    def write_bytes(self, data):
        blob = data.draw(
            st.sampled_from([b"\x00\x01\n", b"raw bytes", b"", b"nul\x00end"])
        )
        self.session.write(BIN, blob)
        with self._raw["open"](self.b_root / BIN, "wb") as fh:
            fh.write(blob)

    @rule(data=st.data())
    def open_for_write_or_append(self, data):
        path = data.draw(st.sampled_from(FILES + [BIN]))
        mode = data.draw(st.sampled_from(["w", "a"]))
        try:
            with open(self.a_root / path, mode) as fh:
                fh.write("opened\n")
            failed = False
        except OSError:
            failed = True
        if failed:
            # the view refuses what the filesystem refuses: the
            # parent dir is gone on both sides after rmtree
            with pytest.raises(OSError):
                with self._raw["open"](self.b_root / path, mode) as fh:
                    fh.write("opened\n")
            return
        self.raw_makedirs((self.b_root / path).parent)
        with self._raw["open"](self.b_root / path, mode) as fh:
            fh.write("opened\n")

    @rule(path=st.sampled_from(FILES), frag=parent_fragments, repl=replacements)
    def edit(self, path, frag, repl):
        oracle = self.oracle_read(path)
        if oracle is None:
            with pytest.raises(FileNotFoundError):
                self.session.edit(path, frag, repl)
            return
        text = oracle.decode()
        if frag in text:
            self.session.edit(path, frag, repl)
            with self._raw["open"](self.b_root / path, "w") as fh:
                fh.write(text.replace(frag, repl))
        else:
            with pytest.raises(ValueError):
                self.session.edit(path, frag, repl)

    @rule(path=st.sampled_from(FILES))
    def delete(self, path):
        # the session's own world decides the error contract:
        # a staged deletion is an idempotent no-op, an untracked
        # absent file is an error, anything else deletes for real
        view = None
        try:
            view = self.session.read(path)
        except FileNotFoundError:
            view = None
        staged = self.session.staged().get(
            self.session.canon(path), "absent"
        )
        if staged is None:
            self.session.delete(path)
        elif view is None:
            with pytest.raises(FileNotFoundError):
                self.session.delete(path)
        else:
            self.session.delete(path)
            self._raw["unlink"](self.b_root / path)

    @rule(data=st.data())
    def rename(self, data):
        src = data.draw(st.sampled_from(FILES + MOVE_TARGETS))
        dst = data.draw(st.sampled_from(FILES + MOVE_TARGETS))
        if src == dst:
            return
        if not (self.b_root / src).is_file():
            # view equality means the session agrees the src is absent
            with pytest.raises(FileNotFoundError):
                self.session.rename(src, dst)
            return
        # POSIX: renaming onto an existing file overwrites it
        self.session.rename(src, dst)
        self.raw_makedirs((self.b_root / dst).parent)
        self._raw["rename"](self.b_root / src, self.b_root / dst)

    @rule(data=st.data())
    def copy_file(self, data):
        src = data.draw(st.sampled_from(FILES + [BIN]))
        dst = data.draw(st.sampled_from(["copy.txt", "dir/copy.txt"]))
        if not (self.b_root / src).is_file():
            with pytest.raises(OSError):
                shutil.copyfile(self.a_root / src, self.a_root / dst)
            return
        shutil.copyfile(self.a_root / src, self.a_root / dst)
        self.raw_copyfile(self.b_root / src, self.b_root / dst)

    @rule(data=st.data())
    def move_file(self, data):
        src = data.draw(st.sampled_from(FILES + MOVE_TARGETS))
        dst = data.draw(st.sampled_from(["moved.txt", "dir/moved.txt"]))
        if not (self.b_root / src).is_file():
            return
        shutil.move(str(self.a_root / src), str(self.a_root / dst))
        self.raw_makedirs((self.b_root / dst).parent)
        self._raw["rename"](self.b_root / src, self.b_root / dst)

    @rule(data=st.data())
    def link(self, data):
        target = data.draw(st.sampled_from(["a.txt", "missing.txt"]))
        dst = data.draw(st.sampled_from(["link.txt", "dir/link.txt"]))
        if (self.b_root / dst).is_file() or (self.b_root / dst).is_symlink():
            return
        self.session.symlink(target, dst)
        self.raw_makedirs((self.b_root / dst).parent)
        self._raw["symlink"](target, self.b_root / dst)

    @rule()
    def apply(self):
        self.session.apply()
        a_files = self.tree(self.a_root)
        b_files = self.tree(self.b_root)
        # tree() reads through links on both sides the same way
        assert a_files == b_files
        self.view_equals_oracle()

    def tree(self, root):
        out = {}
        for base, _dirs, names in self._raw["walk"](root):
            for name in names:
                path = Path(base) / name
                if self._raw["islink"](path):
                    out[str(path.relative_to(root))] = (
                        self._raw["readlink"](path).encode()
                    )
                    continue
                with self._raw["open"](path, "rb") as fh:
                    out[str(path.relative_to(root))] = fh.read()
        return out

    @rule()
    def prune(self):
        self.session.prune_unchanged()
        self.view_equals_oracle()

    @invariant()
    def view_matches_oracle(self):
        self.view_equals_oracle()

    @invariant()
    def diff_is_empty_exactly_when_nothing_pending(self):
        # the session's own staged map is the claim under test: the
        # diff must hide nothing and invent nothing
        pending = False
        for path, content in self.session.staged().items():
            base = None
            try:
                if self._raw["islink"](path):
                    base = self._raw["readlink"](path).encode()
                else:
                    with self._raw["open"](path, "rb") as fh:
                        base = fh.read()
            except OSError:
                base = None
            if isinstance(content, str):
                content = content.encode()
            if content != base:
                pending = True
        assert bool(self.session.diff_git().strip()) == pending

    @rule()
    def rmtree_the_dir(self):
        # rmtree stages every file under the tree as deleted;
        # directories are untracked on the session side, so the
        # oracle tolerates the tree already being gone
        shutil.rmtree(self.a_root / "dir")
        if (self.b_root / "dir").is_dir():
            self._raw["rmtree"](self.b_root / "dir")

    @rule(data=st.data())
    def bounded_edit(self, data):
        path = data.draw(st.sampled_from(FILES))
        start = data.draw(st.integers(min_value=1, max_value=3))
        stop = data.draw(st.integers(min_value=1, max_value=3))
        old = data.draw(parent_fragments)
        repl = "R"
        oracle = self.oracle_read(path)
        if oracle is None:
            with pytest.raises(OSError):
                self.session.edit(
                    path, old, repl, start_line=start, stop_line=stop
                )
            return
        # mirror the documented contract: replace every occurrence
        # inside the 1-based inclusive line span, leave matches that
        # cross the boundary alone
        lines = oracle.decode().split("\n")
        segment = "\n".join(lines[start - 1 : stop])
        if old not in segment:
            with pytest.raises(ValueError):
                self.session.edit(
                    path, old, repl, start_line=start, stop_line=stop
                )
            return
        self.session.edit(path, old, repl, start_line=start, stop_line=stop)
        new_segment = segment.replace(old, repl)
        new_lines = lines[: start - 1] + new_segment.split("\n") + lines[stop:]
        with self._raw["open"](self.b_root / path, "w") as fh:
            fh.write("\n".join(new_lines))

    def teardown(self):
        # the patch is process-global: leaking it would stage
        # the next machine's seed writes
        self.restore()


SessionAgainstRealDisk.TestCase.settings = settings(
    max_examples=40, stateful_step_count=25, deadline=None
)

TestSessionAgainstRealDisk = SessionAgainstRealDisk.TestCase


class ScopeMachine(RuleBasedStateMachine):
    @initialize()
    def seed(self):
        self.root = Path(tempfile.mkdtemp())
        self.disk = dict(SEED)
        self.overlay = {}
        for rel, text in SEED.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        # seed first: install() patches pathlib globally, and a
        # seeded write landing in the overlay would desync the model
        self.known = set(FILES)
        # rename detection pairs a delete with an add by content
        # similarity, not by history: a rename, then a write, then a
        # delete still reads as a rename. Modelling that means
        # reimplementing it, so the machine drives the deterministic
        # merge and test_merge.py pins the pairing instead.
        self.session = EditSession(root=self.root, find_renames=False)
        self.restore = vfs_module.install(self.session)
        # scope rules operate through the router: a scope only
        # receives what pyedit.* routes, never direct method calls
        self._prior_session = pyedit.__dict__.get("session")
        setattr(pyedit, "session", self.session)

    def view(self, rel):
        if rel in self.overlay:
            return self.overlay[rel]
        return self.disk.get(rel)

    @rule(path=st.sampled_from(FILES), text=texts)
    def write(self, path, text):
        pyedit.write(path, text)
        self.overlay[path] = text

    @rule(path=st.sampled_from(FILES), frag=parent_fragments, repl=replacements)
    def edit(self, path, frag, repl):
        view = self.view(path)
        if view is None:
            with pytest.raises(FileNotFoundError):
                pyedit.edit(path, frag, repl)
        elif frag in view:
            pyedit.edit(path, frag, repl)
            self.overlay[path] = view.replace(frag, repl)
        else:
            with pytest.raises(ValueError):
                pyedit.edit(path, frag, repl)

    @rule(path=st.sampled_from(FILES))
    def delete(self, path):
        if self.view(path) is not None:
            pyedit.delete(path)
            self.overlay[path] = None
        elif path not in self.overlay:
            with pytest.raises(FileNotFoundError):
                pyedit.delete(path)
        # deleting an already-deleted path is an idempotent no-op

    @rule(data=st.data())
    def rename(self, data):
        src = data.draw(st.sampled_from(FILES))
        dst = data.draw(st.sampled_from(MOVE_TARGETS))
        view = self.view(src)
        # renaming onto an existing target is covered by the root
        # machine against the real filesystem
        if view is None or src == dst or self.view(dst) is not None:
            return
        pyedit.rename(src, dst)
        self.overlay[src] = None
        self.overlay[dst] = view
        self.known.add(dst)

    @rule()
    def apply(self):
        self.session.apply()
        for rel in self.known:
            self.disk[rel] = self.view(rel)
        # apply writes disk; the staged entries persist until pruned

    @rule()
    def prune(self):
        self.session.prune_unchanged()
        for rel in self.known:
            if rel in self.overlay and self.overlay[rel] == self.disk.get(rel):
                del self.overlay[rel]

    @rule(path=st.sampled_from(FILES), repl=scope_replacements)
    def scope_edits_the_last_region(self, path, repl):
        view = self.view(path)
        last = LAST[path]
        if self.disk[path] is None:
            return
        if path in self.overlay and (view is None or last not in view):
            # the parent rewrote or deleted the region: the scope
            # refuses (Collision), fails on disk truth, or no-ops
            # when its result equals the parent's state. Either way
            # this path ends exactly as it was.
            try:
                with VFS():
                    pyedit.edit(path, last, repl)
            except (Collision, ValueError, FileNotFoundError):
                pass
            merged = None
            try:
                merged = pyedit.read(path)
            except FileNotFoundError:
                merged = None
            assert merged == view
            return
        if view is None or last not in view:
            # a parent write was applied: nothing to edit on disk
            return
        with VFS():
            pyedit.edit(path, last, repl)
        merged = pyedit.read(path)
        assert merged == view.replace(last, repl)
        self.overlay[path] = merged

    @rule(path=st.sampled_from(FILES), repl=scope_replacements)
    def scope_edits_the_region_the_parent_changed(self, path, repl):
        first = FIRST[path]
        if self.disk[path] is None:
            return
        if path in self.overlay:
            # the hunk re-anchors while the parent still holds the
            # anchor line (a merged sibling scope's edit does); it
            # refuses, fails on disk truth, or no-ops when the
            # scope's result equals the parent's state. Either way
            # the parent ends untouched or cleanly advanced.
            view = self.view(path)
            if view is None or first not in view:
                # refuses (Collision), fails on disk truth, or no-ops
                # when the scope's result equals the parent's state
                try:
                    with VFS():
                        pyedit.edit(path, first, repl)
                except (Collision, ValueError, FileNotFoundError):
                    pass
                merged = None
                try:
                    merged = pyedit.read(path)
                except FileNotFoundError:
                    merged = None
                assert merged == view
            else:
                # the parent may have changed a line next to this one,
                # and git merges two changes only with an unchanged
                # line between them
                try:
                    with VFS():
                        pyedit.edit(path, first, repl)
                except Collision:
                    assert pyedit.read(path) == view
                else:
                    assert pyedit.read(path) == view.replace(first, repl)
                    self.overlay[path] = view.replace(first, repl)
        else:
            view = self.view(path)
            if first not in view:
                return
            with VFS():
                pyedit.edit(path, first, repl)
            assert pyedit.read(path) == view.replace(first, repl)
            self.overlay[path] = view.replace(first, repl)

    @rule(path=st.sampled_from(FILES))
    def scope_that_raises_discards_whole(self, path):
        view = self.view(path)
        if view is None:
            return

        class Boom(RuntimeError):
            pass

        try:
            with VFS():
                pyedit.edit(path, LAST[path], "S")
                raise Boom()
        except Boom:
            pass
        except (ValueError, FileNotFoundError):
            # the pattern was not on disk; the scope discards itself
            pass
        assert pyedit.read(path) == view

    @invariant()
    def staged_matches_model(self):
        expect = {self.root / rel: content for rel, content in self.overlay.items()}
        assert self.session.staged() == expect

    @invariant()
    def reads_match_model(self):
        for rel in FILES:
            view = self.view(rel)
            if view is None:
                with pytest.raises(FileNotFoundError):
                    pyedit.read(rel)
            else:
                assert pyedit.read(rel) == view
                # reading materializes the disk content into the
                # overlay as a cache; the model mirrors it
                self.overlay.setdefault(rel, view)

    @invariant()
    def diff_is_empty_exactly_when_nothing_changed(self):
        changed = any(
            rel in self.overlay and self.overlay[rel] != self.disk.get(rel)
            for rel in self.known
        )
        assert bool(self.session.diff_git().strip()) == changed

    def teardown(self):
        self.restore()
        if self._prior_session is None:
            pyedit.__dict__.pop("session", None)
        else:
            setattr(pyedit, "session", self._prior_session)


ScopeMachine.TestCase.settings = settings(
    max_examples=40, stateful_step_count=25, deadline=None
)

TestScopeMachine = ScopeMachine.TestCase
