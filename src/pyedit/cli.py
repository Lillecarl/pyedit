"""pyedit command line interface.

Input is a Python edit script, from -s/--script or from stdin. It runs
against an in-memory overlay: the result prints as a unified diff and
nothing is written to disk unless --apply is given. A dry-run's diff is
also saved under a short id in a temp store, printed as a comment around
the diff, so the id alone can apply it later (pyedit apply ID).

--apply and `apply ID` are different things and do not share a surface.
--apply is a modifier on this run: write instead of dry-running.
`apply ID` is a verb of its own: no script runs, and libgit2 replays a
patch pyedit wrote earlier.

Foreign patch formats are script APIs, not input modes. A script calls
pyedit.apply_v4a(text), pyedit.apply_diff_git(text) or
pyedit.apply_diff_unidiff(text) and keeps every other pyedit call
around it.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import pyedit
from pyedit import config, formatter, runner, store
from pyedit.lsppass import LspPassError
from pyedit.skill import render_skill

EXIT_OK = 0
EXIT_SCRIPT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyedit",
        description=(
            "Run an edit script; every file the script touches is captured "
            "in memory and printed as a unified diff. Nothing is written "
            "to disk unless --apply is given."
        ),
        epilog="Run 'pyedit skill' for the agent-facing usage guide.",
    )
    parser.add_argument(
        "-s",
        "--script",
        metavar="FILE",
        help="edit script to run (default: read from stdin, '-' is stdin)",
    )
    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="write staged changes to disk (default: dry-run)",
    )
    parser.set_defaults(stored=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --apply, write even when staged files have syntax problems",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        metavar="FILE",
        help="write the diff to FILE instead of stdout ('-' is stdout)",
    )
    parser.add_argument(
        "-i",
        "--include",
        action="append",
        metavar="GLOB",
        help="only show and apply changes for paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        metavar="GLOB",
        help="never show or apply changes for paths matching GLOB (repeatable)",
    )
    parser.add_argument(
        "-U",
        "--context",
        type=int,
        default=3,
        metavar="N",
        help="lines of context in the diff (default: 3)",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="do not exclude .gitignore paths from glob discovery",
    )
    parser.add_argument(
        "--no-rename-detection",
        action="store_true",
        help=(
            "merging scopes: do not pair a delete with an add of similar "
            "content, so renaming a file in one scope and editing it under "
            "the old name in another collides instead of following"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help=(
            "kill the run after SECONDS and dump all thread stacks to the "
            "pyedit state directory (default: 300; 0 disables)"
        ),
    )
    parser.add_argument(
        "--max-materialized-bytes",
        type=int,
        default=256 * 1024 * 1024,
        metavar="BYTES",
        help=(
            "memory budget for content staged in the overlay "
            "(default: 268435456; 0 disables the limit)"
        ),
    )
    parser.add_argument(
        "--max-materialized-files",
        type=int,
        default=20000,
        metavar="N",
        help=(
            "file count budget for the overlay (default: 20000; 0 disables the limit)"
        ),
    )
    parser.add_argument(
        "-r",
        "--revision",
        default=None,
        metavar="REV",
        help=(
            "edit the tree at REV (a jj revision) instead of the "
            "working copy; --apply amends it in place"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"pyedit {pyedit.__version__}"
    )
    return parser


def read_input(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (input text, mode, filename for tracebacks).

    'script' is python to run; 'stored' is pyedit's own canonical patch,
    replayed from an id. Nothing here sniffs a format: stdin is a
    script, and a patch a script wants to stage goes through
    pyedit.apply_v4a, pyedit.apply_diff_git or
    pyedit.apply_diff_unidiff."""
    if args.stored is not None:
        if args.script:
            raise SystemExit("pyedit: apply ID takes no other input")
        stored = store.resolve(args.stored)
        if stored is None:
            raise SystemExit(
                f"pyedit: no stored dry-run {args.stored!r}; ids are printed "
                "by earlier runs"
            )
        return stored.read_text(), "stored", stored.as_posix()
    if args.script and args.script != "-":
        path = Path(args.script)
        return path.read_text(), "script", path.as_posix()
    if sys.stdin.isatty():
        raise SystemExit(
            "pyedit: no input: pipe an edit script on stdin, or pass --script FILE"
        )
    return sys.stdin.read(), "script", "<stdin>"


def apply_id(rest: list[str]) -> tuple[str, list[str]]:
    """Peel the id off `pyedit apply ID [OPTIONS]`.

    Not named stored_id: main() binds that name for the dry-run id it
    prints, and a module-level function of the same name is shadowed
    for the whole of main.
    """
    if not rest or rest[0].startswith("-"):
        raise SystemExit(
            "pyedit: apply needs a stored id: pyedit apply ID\n"
            "        to write an edit script's changes, use "
            "pyedit -s FILE --apply"
        )
    return rest[0], rest[1:]


def run_skill(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pyedit skill",
        description="Print the pyedit agent skill as markdown.",
    )
    parser.add_argument(
        "file", nargs="?", help="write the skill to this file instead of stdout"
    )
    args = parser.parse_args(rest)
    skill = render_skill()
    if args.file:
        out = Path(args.file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(skill)
    else:
        sys.stdout.write(skill)
    return EXIT_OK


def run_mcp(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pyedit mcp",
        description="Serve pyedit as an MCP server over stdio.",
    )
    parser.parse_args(rest)
    from pyedit import mcp_server

    return mcp_server.serve_stdio()


def run_patterns(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pyedit patterns",
        description="Print pinned tree-sitter query patterns per language.",
    )
    parser.add_argument("language", nargs="?", help="only show this language")
    args = parser.parse_args(rest)
    from pyedit import _patterns

    pins = _patterns.PATTERNS
    if args.language is not None:
        if args.language not in pins:
            parser.error(f"unknown language {args.language!r}; have: {', '.join(pins)}")
        languages = [args.language]
    else:
        languages = list(pins)
    for language in languages:
        for test, pattern in pins[language]:
            sys.stdout.write(f"# {language} ({test})\n{pattern}\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    tokens = sys.argv[1:] if argv is None else list(argv)
    if tokens and tokens[0] == "skill":
        return run_skill(tokens[1:])
    if tokens and tokens[0] == "mcp":
        return run_mcp(tokens[1:])
    if tokens and tokens[0] == "patterns":
        return run_patterns(tokens[1:])
    stored = None
    if tokens and tokens[0] == "apply":
        stored, tokens = apply_id(tokens[1:])

    parser = build_parser()
    args = parser.parse_args(tokens)
    if stored is not None:
        args.stored = stored
        args.apply = True

    from pyedit import watchdog

    watchdog.start(args.timeout)

    text, mode, filename = read_input(args)
    opts = runner.Options(
        include=args.include,
        exclude=args.exclude,
        context=args.context,
        apply=args.apply,
        force=args.force,
        skip_format=(mode != "script"),
        max_bytes=args.max_materialized_bytes or None,
        max_files=args.max_materialized_files or None,
        respect_gitignore=not args.no_gitignore,
        find_renames=not args.no_rename_detection,
        revision=args.revision,
    )

    def stage(session):
        if mode == "stored":
            # pyedit's own patch: git wrote it, so git applies it
            runner.replay_stored(session, text)
        else:
            runner.execute_script(session, text, filename)

    if opts.revision is not None:
        from pyedit import jjrev

        try:
            result = runner.run_revision(opts, opts.revision, stage)
        except jjrev.JjRevError as exc:
            print(f"pyedit: {exc}", file=sys.stderr)
            print("pyedit: nothing was written", file=sys.stderr)
            return EXIT_SCRIPT_ERROR
    else:
        session = runner.new_session(opts)
        try:
            stage(session)
        except Exception:
            traceback.print_exc()
            if mode == "stored":
                print(
                    "pyedit: stored patch failed; nothing was written",
                    file=sys.stderr,
                )
            else:
                print(
                    "pyedit: edit script failed; nothing was written",
                    file=sys.stderr,
                )
            return EXIT_SCRIPT_ERROR

        try:
            result = runner.finish(session, opts)
        except (
            config.ConfigError,
            formatter.FormatterError,
            LspPassError,
            runner.MetaWithoutTarget,
        ) as exc:
            print(f"pyedit: {exc}", file=sys.stderr)
            print("pyedit: nothing was written", file=sys.stderr)
            return EXIT_SCRIPT_ERROR

    if result.actions:
        print(f"pyedit: actions: {', '.join(result.actions)}", file=sys.stderr)
    if result.formatted:
        print(f"pyedit: formatted: {', '.join(result.formatted)}", file=sys.stderr)

    for problem in result.problems:
        print(f"pyedit: syntax: {problem}", file=sys.stderr)

    # dry-runs print the id so it alone can apply them later;
    # applies print the reverse id so it alone can revert. What is
    # printed is for reading; what is stored is git-canonical and
    # replays through libgit2.
    if result.dry_run_id:
        marker = (
            f"# pyedit dry-run {result.dry_run_id} (pyedit apply {result.dry_run_id})\n"
        )
    elif result.undo_id:
        marker = (
            f"# pyedit undo {result.undo_id} "
            f"(pyedit apply {result.undo_id} to revert)\n"
        )
    else:
        marker = ""

    if args.output == "-":
        sys.stdout.write(marker + result.diff + marker)
    else:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.diff)

    if not result.files and not result.meta and not result.history:
        print("pyedit: no changes", file=sys.stderr)
    elif result.op is not None:
        print(
            f"pyedit: amended {opts.revision} (op {result.op}; restore "
            f"with: pyjj op restore {result.op})",
            file=sys.stderr,
        )
        if result.cleanup_op is not None:
            print(
                f"pyedit: working-copy cleanup (op {result.cleanup_op})",
                file=sys.stderr,
            )
        for entry in result.conflicts:
            print(f"pyedit: conflict in {entry}; resolve it there", file=sys.stderr)
    elif opts.revision is not None:
        print(
            f"pyedit: dry-run against {opts.revision}; re-run with --apply to amend",
            file=sys.stderr,
        )
    elif result.dry_run_id:
        print(
            f"pyedit: dry-run saved as {result.dry_run_id} "
            f"(pyedit apply {result.dry_run_id})",
            file=sys.stderr,
        )
    elif args.apply and result.undo_id:
        print(
            f"pyedit: changes applied; undo saved as {result.undo_id} "
            f"(pyedit apply {result.undo_id} to revert)",
            file=sys.stderr,
        )

    # a dry-run warns; an apply of known-broken syntax refuses to write
    # unless --force, in which case the problems are on record anyway
    for key, (old, new) in result.meta.items():
        print(f"pyedit: {key}: {old!r} -> {new!r}", file=sys.stderr)
    for entry in result.history:
        print(
            f"pyedit: committed {entry['id'][:12]} {entry['subject']!r} "
            f"(op {entry['op']}; restore with: pyjj op restore {entry['op']})",
            file=sys.stderr,
        )
        if entry["cleanup_op"] is not None:
            print(
                f"pyedit: working-copy cleanup (op {entry['cleanup_op']})",
                file=sys.stderr,
            )
        for conflict in entry["conflicts"]:
            print(
                f"pyedit: conflict in {conflict}; resolve it there",
                file=sys.stderr,
            )
    if args.apply and result.problems and args.force:
        print("pyedit: applying with syntax problems (--force)", file=sys.stderr)
    if result.refused:
        print(
            "pyedit: syntax check failed; nothing was written "
            "(fix the files, or write them without pyedit)",
            file=sys.stderr,
        )
        return EXIT_SCRIPT_ERROR

    return EXIT_OK
