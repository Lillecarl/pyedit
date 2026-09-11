# pyedit

Scripted multi-file edits with dry-run diffs, built for AI agents.

pyedit runs a Python edit script against a set of input files, captures
every write in an in-memory overlay, and prints the result as a unified
diff. Disk is touched only with `--apply`.

The edit script is plain Python: writes through `open()`, `pathlib`,
`os` and `shutil` are monkeypatched into the overlay, and reads see
staged content (read-your-writes). A session API is also injected as
the global `pyedit`.

## Run

```
pyedit [OPTIONS] [PATH_OR_GLOB ...] < plan.py
```

- `-s, --script FILE`: edit script (default: stdin)
- `-a, --apply`: write staged changes to disk (default: dry-run)
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: filter what is shown
  and applied (repeatable)
- `-U, --context N`: diff context lines (default 3)

Positional arguments are files, directories or glob patterns defining
the input file set.

`pyedit skill` prints the full agent-facing manual as markdown
(`pyedit skill FILE` writes it to a file). That document is the
authoritative usage guide: script model, session API, recipes,
workflow, and limitations.

## Example

```
printf 'for p in Path("src").glob("*.py"):\n    p.write_text(p.read_text().replace("foo", "bar"))\n' \
  | pyedit 'src/**/*.py'
```

Inspect the diff, then rerun with `--apply` to write.

## Development

- Build and test: `nix build --file . pyedit` (runs pytest via
  pytestCheckHook).
- Local test loop: `nix shell --file /etc/nixpkgs python3.pkgs.pytest
  --command sh -c 'PYTHONPATH=src pytest tests -q'`.
