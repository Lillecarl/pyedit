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

## Development

- Build and test: `nix build --file . pyedit` (runs pytest via
  pytestCheckHook).
- Live-source dev loop: `nix develop --file . editable` installs
  pyedit as a PEP-660 editable package pointing at `./src`, then
  `pytest tests -q` runs against the live tree.
- `bin/pyedit` runs the current build in place: how agents, and this
  repo's own maintenance, are expected to use the tool.
- AGENTS.md carries the module map and the repo's ground rules.
