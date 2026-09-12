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
pyedit [OPTIONS] < plan.py
```

- `-s, --script FILE`: edit script (default: stdin)
- `-p, --patch [FILE]`: apply an OpenAI apply_patch (V4A) envelope
  instead of a script; `*** Begin Patch` input on stdin is
  auto-detected
- `-d, --diff [FILE]`: apply a unified diff instead of a script;
  `diff --git` / `--- a/` input on stdin is auto-detected
- `-a, --apply [ID]`: write staged changes to disk (default:
  dry-run). With a stored dry-run id, applies that stored diff
  instead; an applied change saves a reverse diff whose id reverts it
- `--force`: apply even when the syntax check reports problems
- `--timeout SECONDS`: abort a hung run (default 300, 0 disables);
  a timeout dumps every thread's stack to
  `$XDG_STATE_HOME/pyedit/dumps/`
- `--no-gitignore`: let discovery see .gitignored paths
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: filter what is shown
  and applied (repeatable)
- `-U, --context N`: diff context lines (default 3)

A dry-run wraps its diff in `# pyedit dry-run <id>` comments;
`--apply <id>` applies that stored diff later, and an applied run
prints an undo id that reverts it.

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

## Development

- Build and test: `nix build --file . pyedit` (runs pytest via
  pytestCheckHook).
- Live-source dev loop: `nix develop --file . editable` installs
  pyedit as a PEP-660 editable package pointing at `./src`, then
  `pytest tests -q` runs against the live tree.
- `bin/pyedit` runs the current build in place: how agents, and this
  repo's own maintenance, are expected to use the tool.
- AGENTS.md carries the module map and the repo's ground rules.
