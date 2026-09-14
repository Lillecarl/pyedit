"""The agent-facing skill document, printed by `pyedit skill`."""

from __future__ import annotations

from pathlib import Path

SKILL_HEAD = """\
# pyedit

pyedit stages multi-file edits in memory and shows them as diffs.
Nothing touches disk unless you pass `--apply`.

Report breaking bugs at https://github.com/lillecarl/pyedit

## Invocation

    pyedit [OPTIONS]            # edit script on stdin
    pyedit -s SCRIPT [OPTIONS]  # edit script from a file
    pyedit apply ID             # replay a stored dry-run or undo

Python is the only way to describe an edit. To stage a patch somebody
else wrote, call `pyedit.apply_v4a(text)`, `pyedit.apply_diff_git(text)`
or `pyedit.apply_diff_unidiff(text)`
from inside a script; every other pyedit call stays available around it.

`apply ID` is a subcommand, not a flag, because it is not an edit: it
replays a patch pyedit already wrote and stored. No script runs, and
nothing parses the patch in Python -- git wrote it, so git applies it.
`--apply` is the separate thing it sounds like: this run writes instead
of dry-running.

Options:

- `-s, --script FILE`: edit script (default: stdin; `-` is stdin)
- `-a, --apply`: write staged changes to disk (default: dry-run)
- `--force`: with `--apply`, write even when staged files have syntax
  problems
- `-o, --output FILE`: write the diff to FILE instead of stdout
- `-i, --include GLOB` / `-x, --exclude GLOB`: limit shown and applied
  paths (repeatable)
- `-U, --context N`: diff context lines (default 3)
- `--no-gitignore`: do not exclude .gitignore paths from glob discovery
- `--no-rename-detection`: merging scopes, do not pair a delete with an
  add of similar content
- `--timeout SECONDS`: kill the run after SECONDS and dump all thread
  stacks to the pyedit state directory (default 300; 0 disables)

Exit codes: 0 ok, 1 input failed (nothing written), 2 usage error,
124 watchdog timeout (thread stacks in
`$XDG_STATE_HOME/pyedit/dumps`, default `~/.local/state/pyedit/dumps`).
Diffs are git-style (`a/`, `b/`, `/dev/null`) because that is the
format you already read. They are a representation of what changed,
not a patch for another tool: binary and link changes appear as
one-line notes. Replay a diff through the stored ids below, not by
piping the printed text.

## Dry-run ids and undo

Every dry-run with changes stores its diff under a short id and prints
it as a comment at the start and end of the diff:

    # pyedit dry-run ab12cd34 (pyedit apply ab12cd34)
    ... diff ...
    # pyedit dry-run ab12cd34 (pyedit apply ab12cd34)

Apply a stored dry-run later without resending the script:

    pyedit apply ab12cd34

The id is the only way to replay a dry-run. The printed diff is a
representation for you to read, not a patch to feed back in. Stored
patches live in a temp directory the OS reaps; ids are not stable
across reboots.

Every `--apply` run stores the reverse diff the same way and prints it
as a comment, so a second-guessed apply can be reverted:

    # pyedit undo ef01ab23 (pyedit apply ef01ab23 to revert)

    pyedit apply ef01ab23   # reverts what was applied

The undo is a patch against the disk exactly as this run left it:
apply it before anything else touches the files, and undo the latest
apply first. Binary changes undo too: the stored undo carries a real
binary payload even though the printed diff summarises. Undoing an
undo just reapplies the original diff.

## OpenAI apply_patch (V4A)

`pyedit.apply_v4a(text)` stages an envelope. Create, update, delete and
`*** Move to:` renames are supported with the same context fuzzing as
Codex:

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

One entry point per implementation. Pick by what you have:

| you have | call | reads it |
|---|---|---|
| a patch git wrote, exact `@@` numbers | `pyedit.apply_diff_git(text)` | libgit2 |
| a diff you wrote, approximate numbers | `pyedit.apply_diff_unidiff(text)` | the unidiff library |

`apply_diff_git` takes every kind git has a format for: text, binary
payloads, symlinks (120000) and modes. It checks ONE line position per
hunk and never searches, so wrong `@@` numbers fail instead of moving.
A pyedit dry-run or undo patch is exactly this shape.

`apply_diff_unidiff` reads TEXT hunks only, and anchors each one by
searching outward from its line number with a whitespace-insensitive
second pass, so numbers that are close enough still land. Hand it a
binary or symlink section and it raises, naming `apply_diff_git` --
neither one quietly becomes the other.

Both handle create (`--- /dev/null`), delete (`+++ /dev/null`), pure
renames and `\\ No newline at end of file` markers. A `GIT
binary patch` section applies too, through libgit2, which verifies
the payload against the file it patches. The diffs pyedit prints keep
summarising binary changes in one line, since the payload is base85
noise to read. Stored diffs carry the real payload, so a dry-run id
and an undo id both replay a binary change. A printed diff is a
representation for you to read, not a patch to feed back in; the ids
around it are how it replays.

Both APIs fail closed: one hunk that cannot anchor raises, and the
script stops with nothing staged and nothing written. Catch the
exception yourself if you want to continue past a bad hunk.

A pure rename -- a moved file or directory whose content is
unchanged -- renders as git rename headers (`similarity index
100%`) instead of delete+create, and replays through an id.

## Writing scripts

The script is plain Python, run in-process. The global `pyedit` is an
edit session (API below). For the duration of the script, writes and
moves through `open()`, `pathlib`, `os` and `shutil` land in an
in-memory overlay instead of disk; reads see the overlay first, so
read-your-writes holds. Anything you can write in Python works.

### Session API

    pyedit.glob(pattern)             files and symlinks matching a glob;
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
    pyedit.find(path, pattern)       every regex match as
                                     (line, column, text) --
                                     positions for edit ranges
                                     and splice spans
    pyedit.edit_re(path, pattern,    regex replace with edit()'s
                repl, count=-1,      contract (zero matches
                start_line=None,     raise; repl is a re.sub
                stop_line=None)      template with backrefs)
    pyedit.rename_symbol(path,       rename a symbol everywhere,
                old_name,            import-aware; without a position
                new_name,            the definition is found by
                line=None,           old_name (an ambiguous name
                column=None)         lists its candidates and asks
                                     for line/column; with a
                                     position, old_name must match
                                     what it resolves to
    pyedit.rename_module(path,       rename a module file or package
                old_name, new_name)  folder and update importers
    pyedit.references(path,         every occurrence of the symbol;
                name,               located by name or at a given
                line=None,          (line, column); lines are 1-based
                column=None)
    pyedit.splice(path, spans)       apply many position splices in
                                     one pass; each span is
                                     (start_line, start_col,
                                     end_line, end_col,
                                     replacement), lines 1-based,
                                     columns utf-8 byte offsets (the
                                     convention ast reports); spans
                                     must not overlap; returns the
                                     count
    pyedit.free_names(path)          names the module uses but does
                                     not bind: the raw material for
                                     synthesizing an import block
    pyedit.apply_v4a(text)           stage an OpenAI apply_patch (V4A)
                                     envelope; apply_patch(text) is
                                     the same thing under its
                                     ecosystem name
    pyedit.apply_diff_git(text)      stage a git-canonical patch
                                     (libgit2; exact line numbers,
                                     binary and symlinks included)
    pyedit.apply_diff_unidiff(text)  stage a unified diff read by the
                                     unidiff library (text hunks,
                                     anchored by search)
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

    # the command is the SERVER binary, not the CLI wrapper:
    # pyright's is pyright-langserver --stdio
    with pyedit.lsp(["pyright-langserver", "--stdio"],
                    python_path=sys.executable) as lsp:
        lsp.rename_symbol(path, old_name=old, new_name=new)
        lsp.references(path, name=name)

Same resolution rules as the rope functions, but the server does the
symbol search: it handles namespace packages (no __init__.py), which
rope cannot -- a rope rename in a namespace package silently stays
definition-local. The server sees staged content (files reach it as
didOpen/didChange), so renames account for earlier edits in the
session; returned edits are staged like any other edit.
`old_name`/`name` must match the token at the position when one is
given. Heavy servers may need a moment after startup before results
are complete.

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
                                         name, start_line,
                                         start_column, end_line,
                                         end_column, text,
                                         name_line/name_column (the
                                         identifier position) and
                                         `enclosing` (the named node
                                         around it)
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
reproduce what edit one already changed. git merges the scopes, with
disk as the common ancestor, so line numbers never matter and lines
another scope inserted or removed above an edit change nothing:

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

**Two scopes must not edit neighbouring lines.** git merges two
changed regions only when at least one unchanged line separates them.
Edits with nothing unchanged between them collide, and belong in ONE
scope, where they run in order on the staged state. The same goes for
an edit that depends on an earlier edit's result.

Merging refuses instead of guessing. `pyedit.Collision` is raised for
that neighbouring-lines case, and when two scopes change the same
region, one deletes what another changes, both create a path with
different content, or a binary file diverges. The message says which.
A scope whose body raises is discarded whole -- nothing of it merges,
and earlier merges in the parent stand.

A rename is followed: rename a file in one scope, edit it under the
old name in another, and the edit lands on the new name. git pairs the
delete with the add by content similarity, so it is a guess, not a
reading of the ancestor -- `--no-rename-detection` turns it off and
the two collide instead.

## Examples

Targeted replacement across files:

    for p in pyedit.glob("**/*.py"):
        pyedit.edit(p, "old_name", "new_name")

Mechanical pattern rewrites, when the shape is textual and the
semantics live in the caller:

    pyedit.edit_re("src/app.py", r"variables\[\"(\w+)\"\]", r"args.\1")

Structural rewrites with ast + splice -- parse the staged text,
compute spans from the nodes, replace them all in one call:

    import ast
    text = pyedit.read("src/app.py")
    tree = ast.parse(text)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "legacy":
            spans.append((node.lineno, node.col_offset,
                          node.end_lineno, node.end_col_offset, "modern"))
    pyedit.splice("src/app.py", spans)
    # for per-unit rewrites that may fail: one scope per unit, and
    # a failing scope discards only itself

Create, move and prune with stdlib tools:

    Path("src/new.py").write_text("def main(): ...\\n")
    import shutil, os
    shutil.copy("config.toml", "config.toml.bak")
    os.rename("legacy/old.py", "src/old.py")
    shutil.rmtree("build")

## Workflow

1. Dry-run: `pyedit < plan.py` - read the diff; its first and last
   lines carry the dry-run id.
2. Apply without rerunning: `pyedit apply <id>`. Rerunning the
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
  `symlink(target, path)` creates one. An occupied path is refused;
  `symlink(..., force=True)` retargets or replaces it. Link and
  binary changes render as one-line notes in the printed diff, but
  stored diffs carry git's own sections for them, so a dry-run id
  and an undo id both replay them. Printed output fed back in
  applies its hunks and warns, naming each note-only change it
  skipped. Reads and open() cannot follow a staged link -- they
  resolve after apply.
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
