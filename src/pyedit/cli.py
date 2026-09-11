"""pyedit command line interface.

Input is a Python edit script (-s/--script), an OpenAI apply_patch (V4A)
envelope (-p/--patch) or a unified diff (-d/--diff); bare stdin is
auto-detected between the three. Input runs against an in-memory
overlay: the result prints as a unified diff and nothing is written to
disk unless --apply is given. A dry-run's diff is also saved under a
short id in a temp store, printed as a comment around the diff, so the
id alone can apply it later (--apply ID).
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
import traceback
from pathlib import Path

import pyedit
from pyedit import store
from pyedit import vfs
from pyedit.diff import unified_diffs
from pyedit.session import EditSession, display_path
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-s",
        "--script",
        metavar="FILE",
        help="edit script to run (default: read from stdin, '-' is stdin)",
    )
    source.add_argument(
        "-p",
        "--patch",
        nargs="?",
        const="-",
        default=None,
        metavar="FILE",
        help=(
            "apply an OpenAI apply_patch (V4A) envelope from FILE instead of "
            "running a script ('-' is stdin; bare '*** Begin Patch' input on "
            "stdin is auto-detected)"
        ),
    )
    source.add_argument(
        "-d",
        "--diff",
        nargs="?",
        const="-",
        default=None,
        metavar="FILE",
        help=(
            "apply a unified diff from FILE instead of running a script "
            "('-' is stdin; git-style diffs on stdin are auto-detected)"
        ),
    )
    parser.add_argument(
        "-a",
        "--apply",
        nargs="?",
        const=True,
        default=False,
        metavar="ID",
        help=(
            "write staged changes to disk (default: dry-run); with a "
            "dry-run id, apply that stored diff instead"
        ),
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
    """Return (input text, mode, filename for tracebacks); mode is 'patch', 'diff' or 'script'."""
    if isinstance(args.apply, str):
        if args.script or args.patch is not None or args.diff is not None:
            raise SystemExit("pyedit: --apply ID takes no other input")
        stored = store.resolve(args.apply)
        if stored is None:
            raise SystemExit(
                f"pyedit: no stored dry-run {args.apply!r}; ids are printed "
                "by earlier dry-runs"
            )
        return stored.read_text(), "diff", stored.as_posix()
    for mode, option in (("patch", args.patch), ("diff", args.diff)):
        if option is not None and option != "-":
            path = Path(option)
            if not path.is_file() and mode == "diff":
                stored = store.resolve(option)
                if stored is not None:
                    path = stored
            return path.read_text(), mode, path.as_posix()
    if args.script and args.script != "-":
        path = Path(args.script)
        return path.read_text(), "script", path.as_posix()
    if sys.stdin.isatty():
        raise SystemExit(
            "pyedit: no input: pipe a script, patch or diff on stdin, or pass "
            "--script FILE / --patch FILE / --diff FILE"
        )
    text = sys.stdin.read()
    stripped = text.lstrip()
    if stripped.startswith("*** Begin Patch"):
        mode = "patch"
    elif stripped.startswith("# pyedit dry-run") or stripped.startswith(
        "diff --git "
    ) or stripped.startswith("--- "):
        mode = "diff"
    else:
        mode = "script"
    return text, mode, "<stdin>"


def allowed(
    rel: str, include: list[str] | None, exclude: list[str] | None
) -> bool:
    if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    return True


def apply_changes(staged: dict[Path, str | bytes | None]) -> None:
    for path, content in sorted(staged.items()):
        if content is None:
            path.unlink(missing_ok=True)
        elif isinstance(content, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


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

    parser = build_parser()
    args = parser.parse_args(tokens)

    text, mode, filename = read_input(args)
    session = EditSession(
        max_bytes=args.max_materialized_bytes or None,
        max_files=args.max_materialized_files or None,
        respect_gitignore=not args.no_gitignore,
    )
    pyedit.session = session

    if mode == "patch":
        try:
            session.apply_patch(text)
        except Exception:
            traceback.print_exc()
            print("pyedit: patch failed; nothing was written", file=sys.stderr)
            return EXIT_SCRIPT_ERROR
    elif mode == "diff":
        try:
            session.apply_unified_diff(text)
        except Exception:
            traceback.print_exc()
            print("pyedit: unified diff failed; nothing was written", file=sys.stderr)
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
                        {"pyedit": session, "__name__": "__main__"},
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
    staged = {
        path: content
        for path, content in session.staged().items()
        if allowed(display_path(path), args.include, args.exclude)
    }

    diff_text = "".join(
        diff for _, diff in unified_diffs(staged, context=args.context)
    )

    # dry-runs store the diff so the printed id alone can apply it later
    stored_id = store.save(diff_text) if staged and not args.apply else None
    marker = (
        f"# pyedit dry-run {stored_id} (pyedit --apply {stored_id} to apply)\n"
        if stored_id
        else ""
    )

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
            f"(pyedit --apply {stored_id} to apply)",
            file=sys.stderr,
        )

    if args.apply:
        apply_changes(staged)

    return EXIT_OK
