# AGENTS.md

## Ground rule: this codebase is for AI agents

Everything in this repository is read by AI agents as often as by
humans. Keep it that way:

- **Discoverability first.** One module, one job. A name says what it
  does. A new contributor (human or model) should find any feature by
  reading this file and the module map below, without asking.
- **The skill is the contract.** `src/pyedit/skill.py` is the
  agent-facing usage document. When behavior changes, the skill
  changes in the same commit.
- **Tests are the spec.** Find the test that demonstrates a feature
  before changing it. A fix without a pinning test is not done.
- **Errors are loud and precise.** Every failure says what was
  expected, what happened, and where. Silent fallbacks are bugs.

## Editing this repository

Use pyedit itself -- that is the whole point of this tool. The
`bin/pyedit` launcher always runs the current build:

    bin/pyedit -s script.py          dry-run: diff + syntax check
    bin/pyedit --apply               write
    bin/pyedit apply <id>            apply a stored dry-run, or undo

An edit script is the only input. A patch somebody else wrote is
staged from inside a script, with `pyedit.apply_v4a` or
`pyedit.apply_diff`.

If pyedit cannot perform an edit on its own codebase, that is a bug:
file an issue instead of reaching for sed. Watchdog timeouts dump all
thread stacks to `$XDG_STATE_HOME/pyedit/dumps/`.

## Architecture

`ARCHITECTURE.md` explains how the pieces fit: the one invariant, the
shape of a run, why diffs have two audiences, and which parts of the
job belong to libgit2. Read it before changing anything structural.

## Module map

| module | job |
|---|---|
| `src/pyedit/session.py` | `EditSession`: staged multi-file state machine (the engine) |
| `src/pyedit/active.py` | the active-session stack behind `pyedit.*` routing |
| `src/pyedit/merge.py` | `VFS` scopes: independent edits, context-anchored merge, `Collision` |
| `src/pyedit/vfs.py` | stdlib overlay patching so edit scripts see staged state |
| `src/pyedit/cli.py` | arguments, dry-run/apply/undo, syntax gate, watchdog hookup |
| `src/pyedit/diff.py`, `render.py` | git-style unified diffs (libgit2) |
| `src/pyedit/memgit.py` | in-memory git objects and `git apply` (libgit2) |
| `src/pyedit/patch.py` | OpenAI apply_patch (V4A) envelopes, via `pyedit.apply_v4a` |
| `src/pyedit/udiff.py` | unified diff parse and apply (foreign input), via `pyedit.apply_diff` |
| `src/pyedit/gitpatch.py` | apply pyedit's own canonical patch (libgit2) |
| `src/pyedit/store.py` | dry-run/undo id store |
| `src/pyedit/syntax/` | tree-sitter position queries, outlines, syntax checks |
| `src/pyedit/rope.py` | Python refactors: rename_symbol, rename_module, references |
| `src/pyedit/lsp_client.py` | language server bridge (pygls), non-Python editing |
| `src/pyedit/gitignore.py` | .gitignore filtering for discovery |
| `src/pyedit/watchdog.py` | timeout with thread-stack dump |
| `src/pyedit/skill.py` | the agent skill document itself |
| `src/pyedit/vendor/` | vendored 4va primitive, not public API |

Build and dependencies live in `pyproject.toml` (version from
`_version.py`) and the nix expressions (`pyedit/`, root
`default.nix`). `python3Packages.tree-sitter-grammars` provides the
grammars; add a language there plus a `LanguageRules` subclass.
