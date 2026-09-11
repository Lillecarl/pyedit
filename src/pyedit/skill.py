"""The agent-facing skill document, printed by `pyedit skill`."""

SKILL = """\
# pyedit

pyedit stages multi-file edits in memory and shows them as diffs.
Nothing touches disk unless you pass `--apply`.

## Invocation

    pyedit [OPTIONS]            # edit script on stdin
    pyedit -s SCRIPT [OPTIONS]

Options:

- `-s, --script FILE`: edit script (default: stdin; `-` is stdin)
- `-a, --apply`: write staged changes to disk (default: dry-run)
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB`: only show and apply matching paths (repeatable)
- `-x, --exclude GLOB`: skip matching paths (repeatable)
- `-U, --context N`: diff context lines (default 3)

There are no input path arguments. The script works on any path; every
file it touches is captured. Files read but left unchanged never appear
in the diff.

Exit codes: 0 ok, 1 script failed (nothing written), 2 usage error.
Diffs are git-style (`a/`, `b/`, `/dev/null`); text output pipes to
`git apply` or `patch -p1`.

## Writing scripts

The script is plain Python, run in-process. Two things are set up:

1. The global `pyedit`: an edit session (API below).
2. The stdlib is patched for the duration of the script: writes through
   `open()`, `pathlib` (`Path.write_text`, `write_bytes`, `unlink`,
   `rename`, `replace`, `mkdir`, `rmdir`, `glob`, `iterdir`,
   `exists`, `is_file`, `read_text`, `read_bytes`, `open`), `os`
   (`remove`, `unlink`, `rename`, `replace`, `mkdir`, `makedirs`,
   `listdir`, `walk`, `os.path.exists/isfile/isdir`) and `shutil`
   (`copy`, `copy2`, `copyfile`, `copytree`, `move`, `rmtree`) all land
   in an in-memory overlay instead of disk. The overlay is a dict keyed
   by path: a first read of a file proxies through to the filesystem
   and caches the full content (reads are always whole-file), writes
   stay in memory. Readings and listings consult the overlay first, so
   read-your-writes holds everywhere. Use `with` blocks or `close()`
   your files; written content is staged on close.

Any Python you know how to write works. No special DSL required.

### Session API

    pyedit.glob(pattern)             files matching a filesystem glob
                                     (recursive with **, overlay-aware)
    pyedit.read(path) -> str|bytes   staged content if touched, else
                                     disk (read is cached in full)
    pyedit.write(path, content)      stage str or bytes; new paths ok
    pyedit.edit(path, old, new,      replace; ValueError when old is
                count=-1) -> int     absent; returns replacement count
    pyedit.delete(path)              stage deletion
    pyedit.rename(old, new)          stage move (content kept, source
                                     deleted)

Paths may be absolute or relative to the invocation directory.

Both access patterns work identically: the injected `pyedit` global and
`import pyedit` followed by `pyedit.glob(...)` (the module forwards to
the live session).

## Examples

Targeted replacement across files:

    for p in pyedit.glob("**/*.py"):
        pyedit.edit(p, "old_name", "new_name")

Ordinary Python is equivalent:

    from pathlib import Path
    for p in Path("src").glob("*.py"):
        text = p.read_text()
        p.write_text(text.replace("old", "new"))

Create, move and prune with stdlib tools:

    Path("src/new.py").write_text("def main(): ...\\n")
    import shutil, os
    shutil.copy("config.toml", "config.toml.bak")
    os.rename("legacy/old.py", "src/old.py")
    shutil.rmtree("build")

## Workflow

1. Dry-run: `pyedit < plan.py` - read the diff.
2. Scope it if needed: `--include`/`--exclude` (repeatable, fnmatch on
   the displayed paths; filters apply to both the diff and `--apply`).
3. Save with `-o plan.diff` if you want a record.
4. Apply by rerunning the same script with `--apply`. The script runs
   again from scratch against the on-disk state, so keep it
   deterministic.

## Limits

- Subprocesses and raw file descriptors (`os.open`, `os.fdopen`)
  bypass the overlay.
- Reads slurp the whole file; pipes, fifos and devices are not
  suitable inputs.
- Encoding arguments are ignored; staged text is UTF-8.
- Directories are not tracked: `mkdir` succeeds without creating,
  parents of new files are created on `--apply`, `rmdir` is a no-op,
  empty directories never appear in diffs, and files created in a
  directory that does not exist yet show up via `glob`/`read` but not
  in `os.walk`/`listdir` of parent directories.
- `shutil.copy2` does not preserve metadata; mode bits are not staged.
- Binary files stage as bytes; their diffs are one-line summaries, not
  hunks (the diff file is then not `git apply`-compatible).
- `Path.stat()` and `os.stat()` on files created during the run raise;
  `exists()`, `is_file()` and listings are overlay-aware.
"""
