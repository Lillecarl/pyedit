"""The agent-facing skill document, printed by `pyedit skill`.

**Only AI agents read this. It is not documentation for humans.** It
is loaded into a context window, so every line costs tokens in every
session that uses the tool, and prose written to reassure or persuade
a reader is pure cost. Keep it compact:

- state each rule once; no summary repeating a section
- no motivation, no reassurance, no "the point is"
- tables, lists and fragments over sentences
- an example only where it settles something the rule leaves open
- keep a *why* only where it changes a judgement call

README.md is the human-facing document. Put prose there.

**Escapes in this file are load-bearing.** SKILL_HEAD and SKILL_TAIL
are ordinary strings, so a backslash in an example needs doubling:
`\\1` in the source to print `\1`. Getting this wrong is silent --
until 2026-09-14 the edit_re example printed a literal 0x01 where its
backreference belonged. ESCAPES_SECTION is raw for that reason, and
tests/test_skill.py compiles every python block the document prints.
"""

from __future__ import annotations

from pathlib import Path

SKILL_HEAD = """\
# pyedit

Stages multi-file edits in memory, prints them as a diff. Nothing
reaches disk without `--apply`.

Bugs: https://github.com/lillecarl/pyedit

## Invocation

    pyedit [OPTIONS]            # edit script on stdin
    pyedit -s SCRIPT [OPTIONS]  # edit script from a file
    pyedit apply ID             # replay a stored dry-run or undo

A Python edit script is the only way to describe an edit. Patches
somebody else wrote are staged from inside a script (`apply_v4a`,
`apply_diff_git`, `apply_diff_unidiff` below).

`apply ID` is a subcommand, not a flag: no script runs, libgit2
replays a patch pyedit stored. `--apply` is the unrelated modifier --
this run writes instead of dry-running.

| option | |
|---|---|
| `-s, --script FILE` | edit script (default stdin; `-` is stdin) |
| `-a, --apply` | write to disk (default: dry-run) |
| `--force` | with `--apply`, write despite syntax problems |
| `-o, --output FILE` | write the diff to FILE |
| `-i, --include GLOB` / `-x, --exclude GLOB` | limit shown and applied paths (repeatable) |
| `-U, --context N` | diff context lines (default 3) |
| `--no-gitignore` | let glob discovery see .gitignored paths |
| `--no-rename-detection` | merging scopes, do not pair a delete with an add of similar content |
| `--timeout SECONDS` | kill the run and dump thread stacks (default 300; 0 disables) |

Exit: 0 ok, 1 input failed (nothing written), 2 usage, 124 timeout
(stacks in `$XDG_STATE_HOME/pyedit/dumps`, default
`~/.local/state/pyedit/dumps`).

**The printed diff is a representation, not a patch.** git-style
headers (`a/`, `b/`, `/dev/null`); binary and symlink changes are
one-line notes. Replay it by id, never by piping the text back.

## Dry-run ids and undo

Every dry-run with changes stores its patch and prints the id as the
first and last line of the diff:

    # pyedit dry-run ab12cd34 (pyedit apply ab12cd34)

`pyedit apply ab12cd34` stages and writes it without rerunning the
script. Every `--apply` stores the reverse the same way:

    # pyedit undo ef01ab23 (pyedit apply ef01ab23 to revert)

Stored patches are git-canonical, so binary and symlink changes replay
even though the printed diff summarises them. The undo targets disk as
that run left it: undo the latest apply first, before anything else
touches the files. Undoing an undo reapplies. Ids live in a temp
directory the OS reaps; they do not survive a reboot.

## Configuration

`pyedit.toml`, lowest precedence first; a later file overrides
earlier ones per key:

| layer | location |
|---|---|
| config home | `~/.config/pyedit/pyedit.toml` (XDG_CONFIG_HOME; macOS `~/Library/Application Support/pyedit/pyedit.toml`) |
| umbrella | the file a pyedit.toml names in `parent = "../pyedit.toml"`, relative to that file; pointers may chain |
| session root | `pyedit.toml` next to the invocation directory |

`[format]` maps a file suffix to a formatter command. After the
script finishes, staged text with a matching suffix is piped through
stdin and the stdout is re-staged as the final content -- the diff,
the stored patch and undo all show it. `{path}` expands to the
absolute path (for formatters that resolve their own config from a
file name). A formatter that is missing, exits non-zero, or writes
nothing fails the run; nothing is written.

    [format]
    nix = ["nixfmt", "-"]
    py = ["ruff", "format", "--stdin-filename", "{path}", "-"]

Script runs only; `pyedit apply ID` replays the stored patch as is.

## Staging a patch somebody else wrote

| you have | call | reader |
|---|---|---|
| OpenAI apply_patch (V4A) envelope | `pyedit.apply_v4a(text)` | vendored OpenAI primitive |
| a patch git wrote, exact `@@` numbers | `pyedit.apply_diff_git(text)` | libgit2 |
| a diff you wrote, approximate numbers | `pyedit.apply_diff_unidiff(text)` | the unidiff library |

`apply_diff_git`: every kind git has a format for -- text, binary
payloads, symlinks (120000), modes. Checks ONE line position per hunk
and never searches, so wrong `@@` numbers fail rather than move. A
stored dry-run or undo patch is this shape.

`apply_diff_unidiff`: text hunks only, each anchored by searching
outward from its line number with a whitespace-insensitive second
pass, so approximate numbers land. A binary or symlink section raises,
naming `apply_diff_git`; neither quietly becomes the other.

Both: create (`--- /dev/null`), delete (`+++ /dev/null`), pure renames
(rendered as `similarity index 100%`), `\\ No newline at end of file`.
Both fail closed -- one hunk that cannot anchor raises with nothing
staged. Catch it yourself to continue past a bad hunk.

V4A supports create, update, delete and `*** Move to:` renames, with
Codex's context fuzzing:

    *** Begin Patch
    *** Update File: src/app.py
    @@ def greet():
    -print("Hi")
    +print("Hello, world!")
    *** Add File: docs/new.md
    +# New
    *** Delete File: obsolete.txt
    *** End Patch

## Writing scripts

Plain Python, run in-process. The global `pyedit` is the edit session.
`open()`, `pathlib`, `os` and `shutil` writes land in the overlay;
reads see the overlay first, so read-your-writes holds.

Paths are absolute or relative to the session root (the invocation
directory).

### Session API

    pyedit.glob(pattern)             files and symlinks matching a glob;
                                     pathlib.Path objects (str(p) for
                                     string ops); recursive with **,
                                     .gitignore-excluded
    pyedit.read(path) -> str|bytes   staged content if touched, else
                                     disk
    pyedit.read_fd(n)                content piped in on FD n, to EOF
                                     as str (heredocs, below)
    pyedit.write(path, content)      stage str or bytes; new paths ok
    pyedit.edit(path, old, new,      replace old with new; ValueError
                count=-1,            when old is absent. count=-1 is
                start_line=None,     the default and replaces EVERY
                stop_line=None)      occurrence: scope duplicates with
                                     a line range or count=1; returns
                                     the replacement count
    pyedit.delete(path)              stage deletion
    pyedit.rename(old, new)          stage move; a directory moves as a
                                     whole tree, symlinks move as links
    pyedit.symlink(target, path,     stage a symlink; an occupied path
                force=False)         is refused unless force
    pyedit.find(path, pattern)       every regex match as
                                     (line, column, text)
    pyedit.edit_re(path, pattern,    regex replace with edit()'s
                repl, count=-1,      contract (zero matches raise;
                start_line=None,     repl is a re.sub template with
                stop_line=None)      backrefs)
    pyedit.rename_symbol(path,       rename a symbol everywhere,
                old_name,            import-aware; without a position
                new_name,            the definition is found by
                line=None,           old_name (an ambiguous name lists
                column=None)         candidates and asks for
                                     line/column; with a position,
                                     old_name must match what it
                                     resolves to
    pyedit.rename_module(path,       rename a module file or package
                old_name, new_name)  folder and update importers
    pyedit.references(path, name,    every occurrence of the symbol, by
                line=None,           name or at a position; lines are
                column=None)         1-based
    pyedit.splice(path, spans)       many position splices in one pass;
                                     (start_line, start_col, end_line,
                                     end_col, replacement), lines
                                     1-based, columns utf-8 byte
                                     offsets (what ast reports); spans
                                     must not overlap; returns the count
    pyedit.free_names(path)          names the module uses but does not
                                     bind: material for an import block
    pyedit.apply_v4a(text)           stage a V4A envelope
    pyedit.apply_diff_git(text)      stage a git-canonical patch
    pyedit.apply_diff_unidiff(text)  stage a unified diff
    pyedit.node_at(path, line, col)  what is at a position: kind, name,
                                     start/end_line, start/end_column,
                                     text, name_line/name_column, and
                                     `enclosing` (the named node around
                                     it)
    pyedit.outline(path)             every named definition with its
                                     span, ordered by position
    pyedit.check(path)               syntax problems as
                                     (line, column, message)
    pyedit.diff_git(context=3)       staged changes as one diff,
                                     rendered by libgit2
    pyedit.diff_difflib(context=3)   the same, rendered by difflib; git
                                     names the enclosing function after
                                     each @@, difflib does not
    pyedit.apply(paths=None)         write staged state to disk now,
                                     even in a dry-run; prefer --apply
    pyedit.VFS()                     an independent edit scope (below)
    pyedit.lsp(command, ...)         a language server session (below)

Lines are 1-based, columns 0-based.
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
passes bytes through untouched; `pyedit.read_fd(n)` reads FD n to EOF
as str -- no `/dev/fd` path, works on pipes:

    pyedit -s - 3<<'OLD' 4<<'NEW' <<'PY'
    def greet():
        '''Say hi.'''
        print("Hi")
    OLD
    def greet(name="world"):
        '''Say hi.'''
        print(f"Hi, {name}")
    NEW
    pyedit.edit("greet.py", pyedit.read_fd(3), pyedit.read_fd(4))
    PY

Bodies pair with redirections in order: first body to FD 3 (OLD),
second to FD 4 (NEW), last to stdin (the script). One command carries
the program and every payload, byte-exact -- Python holding multiline
strings never becomes a Python literal.

"""

SKILL_TAIL = """\
## Scopes: several edits to one file, without drift

`with pyedit.VFS():` opens an independent scope: inside the block
every `pyedit.*` call edits the scope, and on exit git merges it into
what it was opened in. A scope starts from DISK truth, so each edit is
written against the file as first read, and line numbers never matter.
Scopes nest.

    with pyedit.VFS():
        pyedit.edit("src/app.py", "def parse(cfg):", "def parse(cfg, strict):")
    with pyedit.VFS():
        pyedit.edit("src/app.py", "import json", "import json\\nimport os")
    with pyedit.VFS():
        pyedit.edit("src/app.py", "VERSION = 1", "VERSION = 2")

**Two scopes must not edit neighbouring lines.** git merges two
changed regions only when at least one unchanged line separates them.
Put such edits in ONE scope, where they run in order on the staged
state -- same for an edit depending on an earlier edit's result.

`pyedit.Collision` is raised for that case, and when two scopes change
the same region, one deletes what another changes, both create a path
with different content, or a binary file diverges. The message says
which. A scope whose body raises merges nothing; earlier merges stand.

A rename is followed: rename in one scope, edit under the old name in
another, and the edit lands on the new name. git pairs the delete with
the add by content similarity, so it is a guess --
`--no-rename-detection` makes the two collide instead.

## Other languages: language servers

`rename_symbol`, `rename_module` and `references` are rope-backed and
Python-only. Other languages go through a language server, never
downloaded: the command comes from PATH or is absolute.

    # the SERVER binary, not the CLI wrapper:
    # pyright's is pyright-langserver --stdio
    with pyedit.lsp(["pyright-langserver", "--stdio"],
                    python_path=sys.executable) as lsp:
        lsp.rename_symbol(path, old_name=old, new_name=new)
        lsp.references(path, name=name)

Same resolution rules, but the server does the symbol search and
handles namespace packages (no `__init__.py`), which rope cannot -- a
rope rename there silently stays definition-local. The server sees
staged content via didOpen/didChange; returned edits are staged like
any other. `old_name`/`name` must match the token at a given position.
Heavy servers may need a moment after startup.

## Syntax awareness

Staged text is parsed before it is shown. Problems print to stderr as
`pyedit: syntax: FILE:LINE:COL: message` (compile() for Python,
tree-sitter otherwise) with the source line and a caret. A dry-run
warns and still shows the diff; `--apply` refuses to write and exits
1. This is the post-edit verification step -- no `&& python -m
compile` needed after it. `--force` overrides, keeps the problems on
record, and the write stays revertible.

`node_at`, `outline` and `check` (above) run on staged content.
Verify a target with `node_at` before a range-limited `edit`; use
`outline` to find definitions in a file too big for context.

Languages: Python, JavaScript, TypeScript, TSX, Go, Rust, C, C++,
Bash, JSON, YAML, TOML, Nix, Ruby, Java, Lua, Zig.

## Examples

Mechanical pattern rewrite:

    pyedit.edit_re("src/app.py", r"variables\\[\\"(\\w+)\\"\\]", r"args.\\1")

Structural rewrite with ast + splice -- parse staged text, compute
spans from nodes, replace in one call:

    import ast
    text = pyedit.read("src/app.py")
    spans = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name) and node.id == "legacy":
            spans.append((node.lineno, node.col_offset,
                          node.end_lineno, node.end_col_offset, "modern"))
    pyedit.splice("src/app.py", spans)
    # per-unit rewrites that may fail: one scope per unit, and a
    # failing scope discards only itself

Stdlib tools stage like everything else:

    Path("src/new.py").write_text("def main(): ...\\n")
    shutil.copy("config.toml", "config.toml.bak")
    os.rename("legacy/old.py", "src/old.py")
    shutil.rmtree("build")

## Behavior notes

- `edit()` replaces EVERY occurrence by default (count=-1). A short
  token rewrites the whole file: pass `count=1`, a line range, or a
  longer unique pattern.
- Subprocesses and raw file descriptors (`os.open`, `os.fdopen`)
  bypass the overlay.
- Symlinks are first-class: `rename` moves the link itself,
  `symlink(target, path, force=True)` retargets or replaces. Reads and
  `open()` cannot follow a staged link -- they resolve after apply.
- Directories are not tracked: parents are created on `--apply`; empty
  directories never appear in diffs.
- Binary files stage as bytes. A text-mode handle on one is refused at
  open, where the real filesystem would defer the failure to read.
- `read()` materializes a file into the staged map as a cache;
  `prune_unchanged()` drops entries equal to disk, so reading a file
  does not put it in the diff.
- Discovery globs exclude .gitignore paths (`--no-gitignore` disables).
  Explicit reads and writes still reach ignored paths.
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
