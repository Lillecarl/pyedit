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
predictable without reimplementing the merge.
"""

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, initialize, rule

import pyedit
from pyedit import vfs as vfs_module
from pyedit.merge import Collision, VFS
from pyedit.session import EditSession

FILES = ["a.txt", "b.txt", "dir/c.txt"]
SEED = {"a.txt": "A0\nA1\n", "b.txt": "B0\nB1\n", "dir/c.txt": "C0\nC1\nC2\n"}
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
        self.session = EditSession(root=self.a_root)

    def oracle_read(self, rel):
        try:
            return (self.b_root / rel).read_text()
        except (FileNotFoundError, IsADirectoryError):
            return None

    def every_rel(self):
        rels = set(FILES) | set(MOVE_TARGETS)
        for path in self.session.staged():
            rels.add(str(path.relative_to(self.a_root)))
        for base, _dirs, names in os.walk(self.b_root):
            for name in names:
                rels.add(str(Path(base).relative_to(self.b_root) / name))
        return sorted(rels)

    def view_equals_oracle(self):
        # the oracle tree is the view: the view-equality invariant
        # makes a path's existence agree on both sides, so reads
        # raise or agree per path
        for rel in self.every_rel():
            oracle = self.oracle_read(rel)
            if oracle is None:
                with pytest.raises(FileNotFoundError):
                    self.session.read(rel)
            else:
                assert self.session.read(rel) == oracle

    @rule(path=st.sampled_from(FILES), text=texts)
    def write(self, path, text):
        self.session.write(path, text)
        (self.b_root / path).write_text(text)

    @rule(path=st.sampled_from(FILES), frag=parent_fragments, repl=replacements)
    def edit(self, path, frag, repl):
        oracle = self.oracle_read(path)
        if oracle is None:
            with pytest.raises(FileNotFoundError):
                self.session.edit(path, frag, repl)
            return
        if frag in oracle:
            self.session.edit(path, frag, repl)
            (self.b_root / path).write_text(oracle.replace(frag, repl))
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
            (self.b_root / path).unlink()

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
        os.rename(self.b_root / src, self.b_root / dst)

    @rule()
    def apply(self):
        self.session.apply()
        a_files = self.tree(self.a_root)
        b_files = self.tree(self.b_root)
        assert a_files == b_files
        for rel, content in b_files.items():
            assert (self.a_root / rel).read_text() == content
        self.view_equals_oracle()

    @rule()
    def prune(self):
        self.session.prune_unchanged()
        self.view_equals_oracle()

    def tree(self, root):
        out = {}
        for base, _dirs, names in os.walk(root):
            for name in names:
                path = Path(base) / name
                out[str(path.relative_to(root))] = path.read_text()
        return out

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
            if path.is_file():
                base = path.read_text()
            if content != base:
                pending = True
        assert bool(self.session.diff().strip()) == pending

    def teardown(self):
        pass


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
        self.session = EditSession(root=self.root)
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
            # the parent ends exactly as it was.
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
                with VFS():
                    pyedit.edit(path, first, repl)
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
            rel in self.overlay and self.overlay[rel] != self.disk[rel]
            for rel in FILES
        )
        assert bool(self.session.diff().strip()) == changed

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
