"""The agent-facing skill document, printed by `pyedit skill`."""

from __future__ import annotations

from pathlib import Path

SKILL_HEAD = """\
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
- `--force`: with `--apply`, write even when staged files have syntax
  problems
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: limit shown and applied
  paths (repeatable)
- `-U, --context N`: diff context lines (default 3)
- `--no-gitignore`: do not exclude .gitignore paths from glob discovery
- `--timeout SECONDS`: kill the run after SECONDS and dump all thread
  stacks to the pyedit state directory (default 300; 0 disables)

Exit codes: 0 ok, 1 input failed (nothing written), 2 usage error,
124 watchdog timeout (thread stacks in
`$XDG_STATE_HOME/pyedit/dumps`, default `~/.local/state/pyedit/dumps`).
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

`-d ab12cd34` also accepts a stored id and re-renders the diff as a
fresh dry-run with a fresh id. Iterate by feeding the commented output
back on stdin: the comments parse as diff comments and a fresh dry-run
prints a fresh id. Stored diffs live in a temp directory the OS reaps;
ids are not stable across reboots.

Every `--apply` run stores the reverse diff the same way and prints it
as a comment, so a second-guessed apply can be reverted:

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

Both structured inputs fail closed: one hunk that cannot anchor
fails the whole input -- nothing is staged, nothing is written, exit
1. With `--force` the files whose hunks fail are SKIPPED (a warning
per skip on stderr) and the rest applies; scripts stay fail-closed,
since a script can write its own try/except.

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
    pyedit.edit(path, old, new,      replace old with new; ValueError
                count=-1,            when old is absent. count=-1 is
                start_line=None,     the default and replaces EVERY
                stop_line=None)      occurrence: scope duplicates with
                                     a line range or count=1; returns
                                     the replacement count
    pyedit.delete(path)              stage deletion
    pyedit.rename(old, new)          stage move (content kept, source
                                     deleted); a directory moves as a
                                     whole tree, symlinks move as
                                     links
    pyedit.symlink(target, path)     stage a symlink; the target is
                                     stored as the link's target
    pyedit.rename_symbol(path,       rename the symbol at (1-based
                line, column,        line, 0-based column) everywhere
                old_name, new_name)  import-aware; old_name must match
                                     what the position resolves to
    pyedit.rename_module(path,       rename a module file or package
                old_name, new_name)  folder and update importers
    pyedit.references(path,          every occurrence of the symbol at
                line, column, name)  the position
    pyedit.apply_v4a(text)           stage an OpenAI apply_patch (V4A)
                                     envelope; apply_patch(text) is
                                     the same thing under its
                                     ecosystem name
    pyedit.apply_diff(text)          stage a unified diff
    pyedit.diff(context=3)           the staged changes as one diff
                                     text
    pyedit.apply(paths=None)         write the staged state to disk
                                     now, even in a dry-run; prefer
                                     the CLI's --apply

Paths may be absolute or relative to the session root (the invocation
directory in CLI runs and edit scripts).
"""

# backslash examples must survive verbatim, so this section is raw;
# writing it inside the main literal would decode the examples away
ESCAPES_SECTION = r"""### Escapes in edit patterns

`edit` matches after Python decodes your string literal: the literal
must evaluate to the file's exact characters. Levels stack up when the
target is code that writes code (shell inside Python, Nix inside
Nix): the file's bytes may hold two backslashes where a rendered view
shows one, and each wrong guess fails as a plain `pattern not found`.

- Get the ground truth first: in the script,
  `print(repr(pyedit.read(path)), file=sys.stderr)` on a dry-run shows
  every backslash; so does `od -c` in a shell. Trust bytes over what a
  tool displayed.
- Build the literal from that: file bytes `\033` (one backslash) is
  `'\\033'` or `r'\033'`; file bytes `\\033` (two) is `'\\\\033'` or
  `r'\\033'`.
- A raw string freezes what you type; it does not tell you how many
  backslashes the file has.
- Prefer an anchor without backslashes at all: match the plain lines
  around the target and put the escape-heavy line, counted from
  evidence, inside the replacement.

### Content on extra file descriptors

Or sidestep decoding entirely: keep big or escape-heavy payloads out
of the script and attach them as extra heredocs. A quoted heredoc
passes bytes through untouched; the script reads them verbatim:

    pyedit -s - 3<<'EOF3' <<'PY'
    printf '\033[31m done'
    EOF3
    old = open("/dev/fd/3").read()
    pyedit.edit("a.py", old, old.replace("done", "OK"))
    PY

With two heredocs, the first redirection gets the first body.
One bash command carries the program and the content -- and the
pattern arrives byte-exact, with no escaping level to lose.

"""

SKILL_TAIL = """\
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

## Syntax awareness

Staged text is parsed before it is shown: syntax problems print to
stderr as `pyedit: syntax: FILE:LINE:COL: message` (compile() for
Python, tree-sitter grammars for others) followed by the source
line with a caret under the position, CPython style. A dry-run
warns and still shows the diff; an `--apply` with syntax problems
refuses to write and exits 1 -- this is the post-edit verification
step, so an agent does not need `&& python -m compile` after it.
Override with `--force`, which keeps the problems on record and
the write revertible. Position queries run on staged content:

    pyedit.node_at(path, line, column)   what is at a position: kind,
                                         name, span, source text and
                                         `enclosing` (the named node
                                         around it, e.g. the method
                                         the position sits in)
    pyedit.outline(path)                 every named definition with
                                         its span, ordered by position
    pyedit.check(path)                   syntax problems: list of
                                         (line, column, message)

Lines are 1-based, columns 0-based. Supported languages: Python,
JavaScript, TypeScript, TSX, Go, Rust, C, C++, Bash, JSON, YAML, TOML,
Nix, Ruby, Java, Lua, Zig. Verify a target with `node_at` before a
range-limited `edit`, and use `outline` to find definitions when the
file is bigger than what fits in context.

## Scopes: several edits to one file, without drift

You are always editing inside a root VFS: the session itself. `with
pyedit.VFS():` opens an independent scope -- no variable needed,
inside the block every `pyedit.*` call edits the scope, and when the
body ends the scope merges into what it was opened in.

The point is making several edits to the SAME file without the edits
stepping on each other: write each one against the file as you first
read it. A scope starts from DISK truth, so edit two does not have to
reproduce what edit one already changed; at merge time every hunk
re-anchors by context, so lines another scope inserted or removed
above it change nothing:

    with pyedit.VFS():
        pyedit.edit("src/app.py", "def parse(cfg):", "def parse(cfg, strict):")
    with pyedit.VFS():
        pyedit.edit("src/app.py", "import json", "import json\\nimport os")
    with pyedit.VFS():
        pyedit.edit("src/app.py", "VERSION = 1", "VERSION = 2")
    # all three merge; the dry-run diff shows the combined result

Plain sequential edits would force the second and third to match the
text the first left behind. Scopes nest; each merges into the scope
it was opened in.

Merging re-anchors hunks by context; line numbers are ignored. It
refuses instead of guessing: `pyedit.Collision` is raised when a hunk
cannot find its context, matches several places, or two scopes
changed the same region incompatibly (delete/edit, double-create and
binary conflicts included). A scope whose body raises is discarded
whole -- nothing of it merges, and earlier merges in the parent
stand. Edits that depend on an earlier edit's result belong in the
same scope, where they run in order on the staged state.

## Examples

Targeted replacement across files:

    for p in pyedit.glob("**/*.py"):
        pyedit.edit(p, "old_name", "new_name")

Create, move and prune with stdlib tools:

    Path("src/new.py").write_text("def main(): ...\\n")
    import shutil, os
    shutil.copy("config.toml", "config.toml.bak")
    os.rename("legacy/old.py", "src/old.py")
    shutil.rmtree("build")

## Workflow

1. Dry-run: `pyedit < plan.py` - read the diff; its first and last
   lines carry the dry-run id.
2. Apply without rerunning: `pyedit --apply <id>`. Rerunning the
   script with `--apply` also works; a script runs from scratch
   against disk, so keep it deterministic.
3. Filter a wide diff with `--include`/`--exclude`; several
   independent edits in one script go through scopes (below).
4. Second-guessed an apply? Its diff carries an undo id;
   `pyedit --apply <undo-id>` reverts.

## Behavior notes

- `edit()` replaces EVERY occurrence of `old` by default
  (count=-1). A short token rewrites the whole file: pass
  `count=1`, a line range, or a longer unique pattern.
- Subprocesses and raw file descriptors (`os.open`, `os.fdopen`)
  bypass the overlay.
- Symlinks are first-class: `rename` moves the link itself and
  `symlink()` creates one. Link changes render as one-line notes
  in the diff (like binaries), so `git apply` cannot recreate
  them and a stored-id undo cannot replay them; pyedit's own
  `--apply` handles both exactly. A diff input carrying notes
  (an undo, re-fed output) applies its hunks and warns, naming
  each note-only change it skipped. Reads and open() cannot
  follow a staged link -- they resolve after apply.
- Directories are not tracked: parents of new files are created on
  `--apply`; empty directories never appear in diffs.
- Binary files stage as bytes; their diffs are one-line summaries.
  A text-mode handle on a binary file is refused at open with a
  clear message; the real filesystem would defer the failure to
  read time.
- `read()` materializes a file into the staged map as a cache;
  `prune_unchanged()` drops entries whose content equals disk.
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


SKILL = SKILL_HEAD + ESCAPES_SECTION + SKILL_TAIL


def render_skill() -> str:
    source = Path(__file__).resolve().parent
    return SKILL + "\n" + SOURCE_SECTION.format(source=source)
