# pyedit

Scripted multi-file edits with dry-run diffs, built for AI agents.

pyedit runs a Python edit script; every file the script touches is
captured in an in-memory overlay (first reads proxy through to the
filesystem and cache the full content, writes stay in memory) and the
result prints as a unified diff. Disk is touched only with `--apply`.

The edit script is plain Python: writes through `open()`, `pathlib`,
`os` and `shutil` are monkeypatched into the overlay, and reads see
staged content (read-your-writes). A session API is also injected as
the global `pyedit`.

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
- `-a, --apply`: write staged changes to disk (default: dry-run)
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: filter what is shown
  and applied (repeatable)
- `-U, --context N`: diff context lines (default 3)

There are no input path arguments: the script works on any path, and
files read but left unchanged never appear in the diff.

`pyedit skill` prints the full agent-facing manual as markdown
(`pyedit skill FILE` writes it to a file). That document is the
authoritative usage guide: script model, session API, recipes,
workflow, and limitations.

## Example

```
printf 'from pathlib import Path\nfor p in Path("src").glob("*.py"):\n    p.write_text(p.read_text().replace("foo", "bar"))\n' \
  | pyedit
```

Inspect the diff, then rerun with `--apply` to write.

## Development

- Build and test: `nix build --file . pyedit` (runs pytest via
  pytestCheckHook).
- Local test loop: `nix shell --file /etc/nixpkgs python3.pkgs.pytest
  --command sh -c 'PYTHONPATH=src pytest tests -q'`.
