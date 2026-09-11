"""The agent-facing skill document, printed by `pyedit skill`."""

SKILL = """\
# pyedit

pyedit stages multi-file edits in memory and shows them as diffs.
Nothing touches disk unless you pass `--apply`.

## Invocation

    pyedit [OPTIONS]            # script, patch or diff on stdin
    pyedit -s SCRIPT [OPTIONS]  # python script from a file
    pyedit -p PATCH [OPTIONS]   # OpenAI apply_patch envelope from a file
    pyedit -d DIFF [OPTIONS]    # unified diff from a file

Options:

- `-s, --script FILE`: edit script (default: stdin; `-` is stdin)
- `-p, --patch [FILE]`: apply an OpenAI apply_patch (V4A) envelope from
  FILE instead of running a script (`-` is stdin). Input starting with
  `*** Begin Patch` on stdin is auto-detected as a patch.
- `-d, --diff [FILE]`: apply a unified diff (git-style) from FILE
  instead of running a script (`-` is stdin). Input starting with
  `diff --git` or `--- a/` on stdin is auto-detected as a diff.
- `-a, --apply`: write staged changes to disk (default: dry-run)
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB`: only show and apply matching paths (repeatable)
- `-x, --exclude GLOB`: skip matching paths (repeatable)
- `-U, --context N`: diff context lines (default 3)
- `--no-gitignore`: do not exclude .gitignore paths from glob discovery
- `--max-materialized-bytes BYTES`: memory budget for staged content
  (default 268435456; 0 disables)
- `--max-materialized-files N`: file count budget for the overlay
  (default 20000; 0 disables)

There are no input path arguments. The script works on any path; every
file it touches is captured. Files read but left unchanged never appear
in the diff.

Exit codes: 0 ok, 1 script failed (nothing written), 2 usage error.
Diffs are git-style (`a/`, `b/`, `/dev/null`); text output pipes to
`git apply` or `patch -p1`.

## OpenAI apply_patch (V4A)

Feed a standard `*** Begin Patch` envelope with `--patch` (or just pipe
it in; it is auto-detected). Create, update, delete and `*** Move to:`
renames are supported; context hunks match with the same fuzzing as
Codex. The patch stages into the same overlay, so a dry-run shows the
unified diff and `--apply` writes it.

    *** Begin Patch
    *** Update File: src/app.py
    @@ def greet():
    -print("Hi")
    +print("Hello, world!")
    *** Add File: docs/new.md
    +# New
    *** Delete File: obsolete.txt
    *** End Patch

Python scripts can also stage a patch inline with `pyedit.apply_patch(text)`
(returns the parsed operations). For string-level V4A application without
any filesystem: `pyedit.apply_diff(content, diff)` (alias `apply_4va`),
vendored from the OpenAI agents SDK.

## Unified diffs

`--diff` (or auto-detected `diff --git` / `--- a/` input) reads a normal
unified diff and stages it the same way. Create (`--- /dev/null`), delete
(`+++ /dev/null`), pure renames, `\\ No newline at end of file` markers and
trailing-whitespace fuzz in context are handled. Binary patches and git
extended headers beyond rename are not.

    pyedit --diff changes.diff          # dry-run diff of the diff
    pyedit --diff changes.diff --apply  # write it

Scripts stage diffs inline with `pyedit.apply_unified_diff(text)`.

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
                                     (recursive with **, overlay-aware,
                                     .gitignore-excluded)
    pyedit.read(path) -> str|bytes   staged content if touched, else
                                     disk (read is cached in full)
    pyedit.write(path, content)      stage str or bytes; new paths ok
    pyedit.edit(path, old, new,      replace; ValueError when old is
                count=-1) -> int     absent; returns replacement count
    pyedit.delete(path)              stage deletion
    pyedit.rename(old, new)          stage move (content kept, source
                                     deleted)
    pyedit.apply_patch(text)         stage an OpenAI apply_patch (V4A)
                                     envelope; returns the operations
    pyedit.apply_unified_diff(text)  stage a unified diff; returns the
                                     patched files

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

- Discovery globs (`pyedit.glob`, `Path.glob`) exclude .gitignore
  paths: every .gitignore between the working directory's ancestors
  and the candidate's own directory applies (negations only override
  rules within the same file; .git/info/exclude and the global
  excludesfile are not consulted). Disable with `--no-gitignore`.
  Explicit reads and writes still reach ignored paths.
- Whole-file reads are cached; the overlay enforces budgets
  (--max-materialized-bytes, --max-materialized-files) and fails the
  run before anything is written when they are exceeded. Narrow the
  scope or raise the limits; budgets release when files are deleted,
  rewritten smaller, or dropped as unchanged.
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
