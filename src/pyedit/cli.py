"""pyedit command line interface.

Runs an edit script supplied on stdin or via --script against a set of
input files, shows the staged changes as a unified diff, and writes
nothing to disk unless --apply is given.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import sys
import traceback
from pathlib import Path

from pyedit.diff import unified_diffs
from pyedit.session import EditSession, display_path

EXIT_OK = 0
EXIT_SCRIPT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyedit",
        description=(
            "Run an edit script against input files and print the staged "
            "changes as a unified diff. Nothing is written to disk unless "
            "--apply is given."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        metavar="PATH_OR_GLOB",
        help="files, directories or glob patterns the script can work on (default: .)",
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
    parser.add_argument("--version", action="version", version="pyedit 0.1.0")
    return parser


def read_script(args: argparse.Namespace) -> tuple[str, str]:
    """Return (script text, filename for tracebacks)."""
    if args.script and args.script != "-":
        path = Path(args.script)
        return path.read_text(), path.as_posix()
    if sys.stdin.isatty():
        raise SystemExit(
            "pyedit: no edit script: pipe a script on stdin or pass --script FILE"
        )
    return sys.stdin.read(), "<stdin>"


def expand_inputs(patterns: list[str]) -> list[Path]:
    files: set[Path] = set()
    for pattern in patterns:
        direct = Path(pattern)
        candidates = [direct] if direct.exists() else [
            Path(m) for m in glob.glob(pattern, recursive=True)
        ]
        for candidate in candidates:
            if candidate.is_dir():
                files.update(walk_files(candidate))
            elif candidate.is_file():
                files.add(candidate)
    return sorted(files)


def walk_files(directory: Path):
    for item in sorted(directory.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            yield from walk_files(item)
        elif item.is_file():
            yield item


def allowed(
    rel: str, include: list[str] | None, exclude: list[str] | None
) -> bool:
    if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    return True


def apply_changes(staged: dict[Path, str | None]) -> None:
    for path, content in sorted(staged.items()):
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    script, filename = read_script(args)
    session = EditSession(expand_inputs(args.paths))

    try:
        exec(
            compile(script, filename, "exec"),
            {"pyedit": session, "__name__": "__main__"},
        )
    except Exception:
        traceback.print_exc()
        print("pyedit: edit script failed; nothing was written", file=sys.stderr)
        return EXIT_SCRIPT_ERROR

    session.prune_unchanged()
    staged = {
        path: content
        for path, content in session.staged().items()
        if allowed(display_path(path), args.include, args.exclude)
    }

    diff_text = "".join(
        diff for _, diff in unified_diffs(staged, context=args.context)
    )

    if args.output == "-":
        sys.stdout.write(diff_text)
    else:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(diff_text)

    if not staged:
        print("pyedit: no changes", file=sys.stderr)

    if args.apply:
        apply_changes(staged)

    return EXIT_OK
