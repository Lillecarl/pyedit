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
import fnmatch
import sys
import traceback
from pathlib import Path

import pyedit
from pyedit import config
from pyedit import formatter
from pyedit import store
from pyedit import gitpatch as _gitpatch
from pyedit import vfs
from pyedit.diff import replayable_patch, unified_diffs, original
from pyedit.session import Symlink
from pyedit.session import EditSession, display_path
from pyedit.skill import render_skill
from pyedit.syntax import render

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
            "file count budget for the overlay (default: 20000; "
            "0 disables the limit)"
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
            "pyedit: no input: pipe an edit script on stdin, or pass "
            "--script FILE"
        )
    return sys.stdin.read(), "script", "<stdin>"


def allowed(
    rel: str, include: list[str] | None, exclude: list[str] | None
) -> bool:
    if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    return True


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


def main(argv: list[str] | None = None) -> int:
    tokens = sys.argv[1:] if argv is None else list(argv)
    if tokens and tokens[0] == "skill":
        return run_skill(tokens[1:])
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
    session = EditSession(
        max_bytes=args.max_materialized_bytes or None,
        max_files=args.max_materialized_files or None,
        respect_gitignore=not args.no_gitignore,
        find_renames=not args.no_rename_detection,
    )
    pyedit.session = session

    if mode == "stored":
        # pyedit's own patch: git wrote it, so git applies it
        try:
            _gitpatch.apply_patch(session, text)
        except Exception:
            traceback.print_exc()
            print(
                "pyedit: stored patch failed; nothing was written", file=sys.stderr
            )
            return EXIT_SCRIPT_ERROR
    else:
        previous_dont_write = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            try:
                restore = vfs.install(session)
                try:
                    exec(
                        compile(text, filename, "exec"),
                        {"pyedit": pyedit, "__name__": "__main__"},
                    )
                finally:
                    restore()
            except Exception:
                traceback.print_exc()
                print(
                    "pyedit: edit script failed; nothing was written",
                    file=sys.stderr,
                )
                return EXIT_SCRIPT_ERROR
        finally:
            sys.dont_write_bytecode = previous_dont_write

    session.prune_unchanged()

    def selected() -> dict[Path, str | bytes | None]:
        return {
            path: content
            for path, content in session.staged().items()
            if allowed(display_path(path), args.include, args.exclude)
        }

    staged = selected()

    # config-driven formatter pass: stdout is re-staged as the final
    # content, so the diff, the stored patch and undo all carry it.
    # script runs only -- `pyedit apply ID` replays what was stored
    if mode == "script":
        try:
            conf = config.load(session.root)
            touched = (
                formatter.format_staged(session, staged, conf.formatters)
                if conf.formatters and staged
                else []
            )
        except (config.ConfigError, formatter.FormatterError) as exc:
            print(f"pyedit: {exc}", file=sys.stderr)
            print("pyedit: nothing was written", file=sys.stderr)
            return EXIT_SCRIPT_ERROR
        if touched:
            listing = ", ".join(
                f"{display_path(p)} ({p.suffix.lstrip('.')})" for p in touched
            )
            print(f"pyedit: formatted: {listing}", file=sys.stderr)
            session.prune_unchanged()
            staged = selected()

    # staged text is parsed: syntax problems surface here, in the same
    # run that shows the diff they would produce
    problems: list[tuple[Path, object]] = []
    for path in sorted(staged):
        value = staged[path]
        if not isinstance(value, str) or isinstance(value, Symlink):
            continue
        for problem in session.check(path):
            problems.append((path, problem))
            print(
                f"pyedit: syntax: {display_path(path)}:"
                f"{problem.line}:{problem.column}: {problem.message}",
                file=sys.stderr,
            )
            print(render(problem, staged[path]), file=sys.stderr)

    diff_text = "".join(
        diff for _, diff in unified_diffs(staged, context=args.context)
    )

    # dry-runs store the patch so the printed id alone can apply it
    # later; applies store the reverse so the id alone can revert. What
    # is printed is for reading; what is stored is git-canonical and
    # replays through libgit2.
    stored_text = replayable_patch(staged, context=args.context)
    stored_id = store.save(stored_text) if staged and not args.apply else None
    if stored_id:
        marker = (
            f"# pyedit dry-run {stored_id} (pyedit apply {stored_id})\n"
        )
    else:
        marker = ""

    undo_id = None
    if args.apply and (not problems or args.force):
        undo_id = _prepare_undo(staged, args.context)
        if undo_id:
            marker = f"# pyedit undo {undo_id} (pyedit apply {undo_id} to revert)\n"

    if args.output == "-":
        sys.stdout.write(marker + diff_text + marker)
    else:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(diff_text)

    if not staged:
        print("pyedit: no changes", file=sys.stderr)
    elif stored_id:
        print(
            f"pyedit: dry-run saved as {stored_id} "
            f"(pyedit apply {stored_id})",
            file=sys.stderr,
        )
    elif args.apply and undo_id:
        print(
            f"pyedit: changes applied; undo saved as {undo_id} "
            f"(pyedit apply {undo_id} to revert)",
            file=sys.stderr,
        )

    # a dry-run warns; an apply of known-broken syntax refuses to write
    # unless --force, in which case the problems are on record anyway
    if args.apply and problems and args.force:
        print("pyedit: applying with syntax problems (--force)", file=sys.stderr)
    if args.apply and problems and not args.force:
        print(
            "pyedit: syntax check failed; nothing was written "
            "(fix the files, or write them without pyedit)",
            file=sys.stderr,
        )
        return EXIT_SCRIPT_ERROR

    if args.apply:
        session.apply(staged)

    return EXIT_OK


def _prepare_undo(
    staged: dict[Path, str | bytes | None], context: int
) -> str | None:
    """Render the reverse of what is about to be applied, and store it.

    The undo diff must be rendered before the staged state reaches the
    disk: its old side is the post-apply state, its new side the
    pre-apply disk truth. It is stored and never printed, so binary
    changes carry a real payload here and undo like any other change.
    """
    pre = {path: original(path) for path in staged}
    if not pre:
        return None
    undo_text = replayable_patch(pre, context=context, base=staged)
    if not undo_text:
        return None
    return store.save(undo_text)
