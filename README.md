# pyedit

Scripted multi-file edits with dry-run diffs, built for AI agents.

pyedit runs a Python edit script against a set of input files, collects
every change in memory, and prints the result as a unified diff. Disk is
touched only with `--apply`.

## CLI

```
pyedit [OPTIONS] [PATH_OR_GLOB ...]
```

- `PATH_OR_GLOB`: files, directories or glob patterns the script works
  on. Directories are walked recursively; hidden entries are skipped.
  Default: `.`
- `-s, --script FILE`: edit script. Default: stdin. `-` is stdin.
- `-a, --apply`: write staged changes to disk. Without it: dry-run.
- `-o, --output FILE`: write the diff to FILE instead of stdout.
- `-i, --include GLOB`: only show and apply matching paths (repeatable).
- `-x, --exclude GLOB`: never show or apply matching paths (repeatable).
- `-U, --context N`: diff context lines (default 3).

Exit codes: 0 success, 1 edit script failed (nothing written), 2 usage
error. Diff headers use `a/` and `b/` prefixes, so output can be piped
to `git apply` or `patch -p1`.

## Edit script

The script is normal Python. pyedit injects one global, `pyedit`: an
`EditSession` bound to the input file set. Writes go to an in-memory
overlay; reads see staged content (read-your-writes). Do not
`import pyedit` inside the script; it would shadow the injected object.

```python
for path in pyedit.glob("**/*.py"):
    text = pyedit.read(path)
    pyedit.write(path, text.upper())
```

API on the injected `pyedit` object:

- `files() -> [Path]`: sorted input files.
- `glob(pattern) -> [Path]`: input files whose `cwd`-relative posix path
  matches `pattern` (fnmatch rules; `*` crosses directories).
- `read(path) -> str`: file content, staged version if edited.
- `write(path, text)`: stage text for `path` (str only). New paths are
  allowed; missing parents are created on `--apply`.
- `edit(path, old, new, count=-1) -> int`: replace `old` with `new`.
  Raises `ValueError` when `old` is not present. Returns replacements.
- `delete(path)`: stage a deletion. Raises when the file does not exist.
- `rename(old, new)`: stage a move (copy content, delete source).

Paths may be absolute or relative to the invocation directory.

## Example

```
printf '%s\n' \
  'for p in pyedit.glob("**/*.py"):' \
  '    pyedit.edit(p, "foo.bar", "foo.baz")' \
  | pyedit 'src/**/*.py'
```

Dry-run prints the diff. Re-run with `--apply` (or `--apply -o
changes.diff` to save it) to write.
