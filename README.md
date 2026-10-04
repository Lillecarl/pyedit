# pyedit

Scripted multi-file edits with dry-run diffs, built for AI agents.

pyedit runs a Python edit script; every file the script touches is
captured in an in-memory overlay (first reads proxy through to the
filesystem and cache the full content, writes stay in memory) and the
result prints as a unified diff. Disk is touched only with `--apply`.

The edit script is plain Python: writes through `open()`, `pathlib`,
`os` and `shutil` are monkeypatched into the overlay, and reads see
staged content (read-your-writes). The session API is also available
as the injected global `pyedit`.

## Run

```
pyedit [OPTIONS] < plan.py     # run an edit script
pyedit apply ID                # replay a stored dry-run or undo
```

- `-s, --script FILE`: edit script (default: stdin)
- `-a, --apply`: write staged changes to disk (default: dry-run); an
  applied change saves a reverse patch whose id reverts it
- `--force`: apply even when the syntax check reports problems
- `--timeout SECONDS`: abort a hung run (default 300, 0 disables);
  a timeout dumps every thread's stack to
  `$XDG_STATE_HOME/pyedit/dumps/`
- `--no-gitignore`: let discovery see .gitignored paths
- `--no-rename-detection`: when merging VFS scopes, do not pair a
  delete with an add of similar content
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: filter what is shown
  and applied (repeatable)
- `-U, --context N`: diff context lines (default 3)
- `-r, --revision REV`: edit the tree at a jj revision instead of the
  working copy; `--apply` amends that commit in place (needs the
  optional pyjj dependency)

A Python edit script is the only way to describe an edit. To stage a
patch somebody else wrote -- an OpenAI apply_patch (V4A) envelope or a
unified diff -- call `pyedit.apply_v4a(text)`,
`pyedit.apply_diff_git(text)` (libgit2, exact line numbers, every kind
git has a format for) or `pyedit.apply_diff_unidiff(text)` (the unidiff
library, text hunks anchored by search) from inside a script.

A dry-run wraps its diff in `# pyedit dry-run <id>` comments;
`pyedit apply <id>` applies that stored patch later, and an applied run
prints an undo id that reverts it. That patch is git's own, written
and applied by libgit2, so the replay path parses nothing in Python.

There are no input path arguments: the script works on any path, and
files read but left unchanged never appear in the diff.

`pyedit skill` prints the full agent-facing manual as markdown
(`pyedit skill FILE` writes it to a file). That document is the
authoritative usage guide: script model, session API, scopes,
recipes, workflow, and limitations.

It carries YAML frontmatter, so it is a SKILL.md as agent harnesses
read one. The nix package renders it at build time to

    $out/share/skills/pyedit/pyedit/SKILL.md

for a NixOS or home-manager configuration to link into an agent's
skill directory:

```nix
home.file.".claude/skills/pyedit".source =
  "${pyedit}/share/skills/pyedit/pyedit";
```

## Example

```
cat > plan.py <<'EOF'
pyedit.edit("src/app.py", "def parse(cfg):", "def parse(cfg, strict):")
with pyedit.VFS():
    pyedit.edit("src/app.py", "import json", "import json\nimport os")
EOF
pyedit -s plan.py        # dry-run: diff + syntax check
pyedit --apply           # write
```

Writes through `pathlib` and `shutil` work too -- the overlay catches
them either way.

## Editing older commits

`pyedit -r REV` reads base file content from a jj revision instead of
the working copy. `--apply` amends that commit in place; descendants
rebase, and the run prints a `pyjj op restore` id that reverts the
whole step. The working copy must start clean on every touched path.

Resolve conflict markers where they first appear: point `-r` at the
commit that introduced them instead of patching the tip. This needs
pyjj, an optional dependency the distributor includes or leaves out
at build time; without it `-r` refuses loudly.

## Configuration

pyedit reads `pyedit.toml` when one is present. Three layers, lowest
precedence first; a later file overrides earlier ones per key:

1. the config home: `~/.config/pyedit/pyedit.toml` on Linux
   (`XDG_CONFIG_HOME` respected), `~/Library/Application
   Support/pyedit/pyedit.toml` on macOS
2. the file a pyedit.toml names in `parent = "../pyedit.toml"`,
   resolved relative to that file -- for umbrella collections where
   several repos share one config; pointers may chain
3. `pyedit.toml` at the invocation directory (the session root)

One table is read today, `[format]`: a formatter pass over the staged
text. After the edit script finishes, each staged text file whose
suffix has a command is piped through the formatter's stdin; the
stdout becomes the file's final staged content, so the printed diff,
the stored patch and undo all show the formatted text. `{path}`
expands to the absolute path, for formatters that resolve their own
config from a file name (ruff's `--stdin-filename`, for example).

```toml
[format]
nix = ["nixfmt", "-"]
py = ["ruff", "format", "--stdin-filename", "{path}", "-"]
```

The pass fails closed: a formatter that is missing, exits non-zero or
writes nothing to stdout fails the run and nothing is written. Only
stdin-to-stdout formatters are supported for now; support for
formatters that want a file on disk is tracked on GitHub.

One `[lsp.NAME]` table per language server: after the edit script
finishes and before `[format]`, the server starts once and applies
its code actions in listed order (then formatting when enabled) to
every staged text file with a matching suffix. Results are re-staged
through the session, so the diff, the stored patch and undo all
carry them -- and the syntax check sees the final text. Files that
were only read are never touched: the pass sees staged files, not
the tree.

```toml
[lsp.ruff]
command = ["ruff", "server"]
suffixes = ["py"]
actions = ["source.fixAll.ruff", "source.organizeImports.ruff"]
format = true
```

`actions` are full LSP code action kinds, passed through verbatim;
`format = true` needs a server with formatting capability. Either
key may carry the table alone, but a table with neither does
nothing and fails loudly. A server that will not start, or an action
that fails, fails the run and nothing is written -- the same
fail-closed contract as `[format]`.

`exclude` is a list of globs where neither pass runs. Plain patterns
match the session-root-relative path (`prompt-toolkit/**` spares a
vendored tree inside the session); patterns starting with `/` or
`~` match the absolute path instead (`~/Code/vendor/*` spares whole
checkouts from a config-home file). Later layers replace the list,
like every other key. Excluded files are only skipped by the passes:
they remain editable, diffable and applicable.

```toml
exclude = ["prompt-toolkit/**"]
```

## Development

- Build and test: `nix build --file . pyedit` (runs pytest via
  pytestCheckHook).
- Live-source dev loop: `nix develop --file . editable` installs
  pyedit as a PEP-660 editable package pointing at `./src`, then
  `pytest tests -q` runs against the live tree.
- `bin/pyedit` runs the current build in place: how agents, and this
  repo's own maintenance, are expected to use the tool.
- AGENTS.md carries the module map and the repo's ground rules.
