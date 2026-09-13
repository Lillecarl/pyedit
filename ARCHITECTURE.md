# pyedit architecture

How the pieces fit. `AGENTS.md` says what each module is; this says why
the shape is what it is, and which parts you cannot move without
breaking something.

`src/pyedit/skill.py` is the usage contract. This file is for changing
the code, not for using it.

## The one invariant

**Staged state lives in memory. `EditSession.apply()` is the only thing
that writes to disk.**

Everything else follows. A dry-run is not a second code path: it is a
run that reaches the end without calling `apply()`. Input handling,
merging, syntax checks and diff rendering do not know which kind of run
they are in. The diff is not a prediction of what would happen; it is a
rendering of the state that `apply()` writes.

Break this and the tool loses its reason to exist.

## One run, end to end

    argv
      |
      v
    cli.main
      |
      +-- EditSession()                   the overlay: {path: str|bytes|Symlink|None}
      |
      +-- dispatch on input               exactly one of:
      |     script  -> vfs.install(session), exec(), restore
      |     V4A     -> patch.apply_patch    -> session.write/delete
      |     diff    -> udiff.apply_diff     -> session.write/delete/symlink
      |
      +-- session.prune_unchanged()       a read is not a change
      +-- include/exclude filter
      +-- syntax check on staged text     warns; gates --apply
      |
      +-- diff.unified_diffs(staged)      printed: notes for bytes and links
      +-- diff.unified_diffs(replayable)  stored: git sections that re-apply
      |     store.save -> dry-run id
      |     _prepare_undo -> undo id      (reverse, rendered before the write)
      |
      +-- session.apply()                 only on --apply

The syntax check runs before the diff so one run shows both the problem
and the change that causes it.

The undo diff is rendered **before** `apply()`. Its old side is the
staged state, its new side is disk truth. After the write, disk truth is
gone.

## The overlay

`session.py` holds one dict keyed by resolved path:

| value | means |
|---|---|
| `str` | text content |
| `bytes` | binary content |
| `Symlink(target)` | a symlink |
| `None` | a deletion |

A first read proxies to disk and materialises the whole file into the
dict, so read-your-writes holds. `prune_unchanged()` then drops entries
that match disk, which is why reading a file does not put it in the
diff.

`_slurp` decodes NUL-free bytes to `str`. A file is "binary" because it
contains a NUL, not because of its name.

### Scopes

`merge.py` gives `with pyedit.VFS():` an independent overlay over disk
truth. It sees neither the parent's staged state nor a sibling's. On
exit its changes render as hunks and re-anchor on the parent **by
context alone** -- line numbers are advisory -- so two scopes editing
the same file do not have to account for each other's line shifts.

Ambiguity is a `Collision`, never a guess. A scope whose body raises
merges nothing.

`active.py` is the stack that makes this work: `pyedit.*` routes to the
top of it, the root session normally, a scope inside its `with` body.
Nothing else touches that stack.

### The stdlib patch

`vfs.py` redirects `open`, `pathlib`, `os` and `shutil` into the
overlay, and returns a callable that puts every attribute back.

**It is installed for script input only.** V4A and diff input call the
session API directly and need no patching. Anything that escapes the
patched stdlib -- a subprocess, a raw fd -- escapes the overlay, and
that is documented rather than defended against.

`watchdog.py` captures the real `os` and `open` at import time for
exactly this reason: it must write a stack dump while the process is
patched and dying.

## Two audiences for a diff

This split is deliberate and easy to undo by accident.

**Printed** diffs are a representation for an agent to read. Git-style
headers, because that is the format a model already knows. Binary and
symlink changes appear as one-line notes. They are *not* a patch for
another tool; the ids in the surrounding comments are how a diff gets
replayed.

**Stored** diffs -- dry-run ids and undo ids -- are pyedit talking to
itself. Nobody reads them, so they carry git's own sections for binary
payloads and 120000 symlink modes, and they replay exactly.

`unified_diffs(..., replayable=True)` is the switch. `cli.py` turns it
on only for the stored copies, and only when a note is present, so text
runs render once.

## What git owns, and what pyedit owns

`memgit.py` is libgit2 (through pygit2) with no disk anywhere: a
`Repository` with a mempack object database, no workdir, no index file,
no repository path.

It owns the formats git invented and pyedit has no version of:

- hunk rendering (`render.py` diffs two blobs)
- base85 binary payloads
- symlink entries, which are blobs with mode 120000
- applying both, through `git_apply_to_tree`

pyedit owns parsing and text application, and that is not an accident.
libgit2's parser rejects most of what pyedit is handed:

- a diff with no `diff --git` line -- agents write these constantly
- a create or delete with no `new file mode` / `deleted file mode`
- a zero-hunk section, such as an empty-file create
- pyedit's own `Symlink` and `Binary file` note lines

And libgit2's applier has **no fuzz at all**: one line position,
`memcmp`, no whitespace tolerance. `udiff._find_block` searches outward
from the hint and falls back to whitespace-insensitive matching, which
is what makes an agent's approximate line numbers land.

So `udiff.py` parses everything, applies text hunks itself, and routes a
section to libgit2 only when it declares a binary payload or a 120000
mode (`_PatchedFile.needs_git`).

`memgit.py`'s docstring carries three rules that will segfault or
silently lie if you break them. Read it before adding a call there.

## Semantic editing

`rope.py` does Python renames and reference finding in-process. Rope
reads through the stdlib, so during a script run it sees staged content
through the VFS patch; it is opened with `ropefolder=None` and its
changes are staged, never performed.

`lsp_client.py` is the same position-based API for other languages,
driven by a real language server over stdio. The server binary comes
from the environment and is never downloaded. Staged content reaches it
as `didOpen`/`didChange`.

`syntax/` is tree-sitter for position queries and outlines, and
`compile()` for Python checks. A language with no installed grammar
degrades: checking skips it, position queries raise.

## Rails

- **Fail closed.** One hunk that will not anchor fails the whole input;
  nothing is staged. `--force` downgrades that to per-file skips with a
  warning each. Scripts stay fail-closed because a script can catch its
  own exceptions.
- **Syntax gate.** A dry-run warns. An `--apply` of known-broken syntax
  refuses to write without `--force`.
- **Loud errors.** Every failure says what was expected, what happened
  and where. A silent fallback is a bug, not a kindness.
- **Watchdog.** Timeout kills the run and dumps every thread's stack to
  `$XDG_STATE_HOME/pyedit/dumps`.
- **Stored diffs are 0o600.** They can carry anything the tree does.

## Adding things

| you want to | do it in |
|---|---|
| a new script API | `session.py`, plus `skill.py` in the same commit |
| a new input format | its own module beside `patch.py` / `udiff.py`, staging into the session |
| a new language for checks or positions | `syntax/rules.py` plus the grammar in the nix expression |
| anything needing real git | `memgit.py` -- read its docstring first |
| changing what a diff looks like | `diff.py`, and decide printed or stored |

Tests are the spec. Find the test that demonstrates a behaviour before
you change it, and leave a new one behind that would have caught the
bug you fixed.
