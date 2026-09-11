"""The agent-facing skill document, printed by `pyedit skill`."""

from __future__ import annotations

from pathlib import Path

SKILL = """\
# pyedit

pyedit stages multi-file edits in memory and shows them as diffs.
Nothing touches disk unless you pass `--apply`.

Report breaking bugs at https://github.com/lillecarl/pyedit

## Invocation

    pyedit [OPTIONS]            # script, patch or diff on stdin
    pyedit -s SCRIPT [OPTIONS]  # python script from a file
    pyedit -p PATCH [OPTIONS]   # OpenAI apply_patch envelope from a file
    pyedit -d DIFF [OPTIONS]    # unified diff from a file

Options:

- `-s, --script FILE`: edit script (default: stdin; `-` is stdin)
- `-p, --patch [FILE]`: OpenAI apply_patch (V4A) envelope; input starting
  with `*** Begin Patch` on stdin is auto-detected
- `-d, --diff [FILE]`: unified diff; input starting with `diff --git`,
  `--- a/` or a pyedit dry-run comment on stdin is auto-detected
- `-a, --apply`: write staged changes to disk (default: dry-run); with a
  dry-run id, `--apply ID` applies that stored diff
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: limit shown and applied
  paths (repeatable)
- `-U, --context N`: diff context lines (default 3)
- `--no-gitignore`: do not exclude .gitignore paths from glob discovery

Exit codes: 0 ok, 1 input failed (nothing written), 2 usage error.
Diffs are git-style (`a/`, `b/`, `/dev/null`); the output pipes to
`git apply` or `patch -p1`.

## Dry-run ids and undo

Every dry-run with changes stores its diff under a short id and prints
it as a comment at the start and end of the diff:

    # pyedit dry-run ab12cd34 (pyedit --apply ab12cd34 to apply)
    ... diff ...
    # pyedit dry-run ab12cd34 (pyedit --apply ab12cd34 to apply)

Apply a stored dry-run later without resending the script or diff:

    pyedit --apply ab12cd34

Iterate by feeding the commented output back on stdin: the comments
parse as diff comments and a fresh dry-run prints a fresh id. Stored
diffs live in a temp directory the OS reaps; ids are not stable across
reboots.

Every `--apply` run stores the reverse diff the same way and prints it
as a comment, so a second-guessed apply can be reverted:

    # pyedit undo ef01ab23 (pyedit --apply ef01ab23 to revert)
    ... applied diff ...
    # pyedit undo ef01ab23 (pyedit --apply ef01ab23 to revert)

    pyedit --apply ef01ab23   # reverts what was applied

The undo is a patch against the disk exactly as this run left it:
apply it before anything else touches the files, and undo the latest
apply first. Binary changes cannot be undone (they are skipped with a
note on stderr); undoing an undo just reapplies the original diff.

## OpenAI apply_patch (V4A)

Create, update, delete and `*** Move to:` renames are supported with
the same context fuzzing as Codex:

    *** Begin Patch
    *** Update File: src/app.py
    @@ def greet():
    -print("Hi")
    +print("Hello, world!")
    *** Add File: docs/new.md
    +# New
    *** Delete File: obsolete.txt
    *** End Patch

## Unified diffs

`--diff` handles create (`--- /dev/null`), delete (`+++ /dev/null`),
pure renames and `\\ No newline at end of file` markers. Binary
patches are not supported.

## Writing scripts

The script is plain Python, run in-process. The global `pyedit` is an
edit session (API below). For the duration of the script, writes and
moves through `open()`, `pathlib`, `os` and `shutil` land in an
in-memory overlay instead of disk; reads see the overlay first, so
read-your-writes holds. Anything you can write in Python works.

### Session API

    pyedit.glob(pattern)             files matching a filesystem glob;
                                     returns pathlib.Path objects (use
                                     str(p) for string operations);
                                     recursive with **, .gitignore-
                                     excluded
    pyedit.read(path) -> str|bytes   staged content if touched, else
                                     disk
    pyedit.write(path, content)      stage str or bytes; new paths ok
    pyedit.edit(path, old, new,      replace; ValueError when old is
                count=-1,            absent; returns replacement count;
                start_line=None,     start_line/stop_line bound the
                stop_line=None)      replacement to 1-based inclusive
                                     lines, for duplicate patterns
    pyedit.delete(path)              stage deletion
    pyedit.rename(old, new)          stage move (content kept, source
                                     deleted)
    pyedit.rename_symbol(path,       rename the symbol at (1-based
                line, column,        line, 0-based column) everywhere
                old_name, new_name)  import-aware; old_name must match
                                     what the position resolves to
    pyedit.rename_module(path,       rename a module file or package
                old_name, new_name)  folder and update importers
    pyedit.references(path,          every occurrence of the symbol at
                line, column, name)  the position
    pyedit.apply_patch(text)         stage a V4A envelope
    pyedit.apply_unified_diff(text)  stage a unified diff

Paths may be absolute or relative to the session root (the invocation
directory in CLI runs and edit scripts).

## Other languages: language servers

`rename_symbol`, `rename_module` and `references` above are
rope-backed and Python-only. Other languages go through their language
server, which pyedit never downloads: the command comes from PATH or
is absolute.

    with pyedit.lsp(["rust-analyzer"]) as lsp:
        lsp.rename_symbol(path, line, column, old_name, new_name)
        lsp.references(path, line, column, name)

Same position-based API as the rope functions. The server sees staged
content (files reach it as didOpen/didChange), so renames account for
earlier edits in the session; returned edits are staged like any other
edit. `old_name`/`name` must match the token at the position. Heavy
servers may need a moment after startup before results are complete.

## Independent scopes: several edits, merged by context

Wrap independent edits in their own scopes; each sees the pristine
tree, so an edit never has to account for lines another edit moved:

    with pyedit.VFS() as root:      # collector
        with pyedit.VFS(root) as fs:
            fs.edit("a.py", old_a, new_a)
        with pyedit.VFS(root) as fs:
            fs.edit("b.py", old_b, new_b)
        # both merged here

Scopes see disk truth, not each other. Edits that depend on an
earlier edit belong in the SAME scope, where they run in order on the
staged state.

Merging re-anchors hunks by context; line numbers are ignored. It
refuses instead of guessing: `pyedit.Collision` is raised when a hunk
cannot find its context, matches several places, or two scopes
changed the same region incompatibly (delete/edit, double-create and
binary conflicts included). On a collision the collector keeps the
earlier merges; if the exception escapes the collector's with-body,
everything is discarded. `root.apply()` writes the merged state to
disk; `root.diff()` renders it.

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
2. Scope it if needed: `--include`/`--exclude`.
3. Apply by rerunning the same script with `--apply`. The script runs
   again from scratch against the on-disk state, so keep it
   deterministic.

## Behavior notes

- Subprocesses and raw file descriptors (`os.open`, `os.fdopen`)
  bypass the overlay.
- Directories are not tracked: parents of new files are created on
  `--apply`; empty directories never appear in diffs.
- Binary files stage as bytes; their diffs are one-line summaries.
- Discovery globs exclude .gitignore paths; disable with
  `--no-gitignore`. Explicit reads and writes still reach ignored
  paths.
"""

SOURCE_SECTION = """\
## Source

`import pyedit` works in scripts and library code; the installed
package lives at:

    {source}

If pyedit breaks - a crash, a wrong diff, a failed apply - report it
at https://github.com/lillecarl/pyedit
"""


def render_skill() -> str:
    source = Path(__file__).resolve().parent
    return SKILL + SOURCE_SECTION.format(source=source)
