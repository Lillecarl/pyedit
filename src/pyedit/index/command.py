"""`pyedit index`: build the index, or ask it in SQL. Results are JSON
on stdout; warnings and progress go to stderr."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pyedit.index import build, embed, gather, gitstore, sql
from pyedit.session import EditSession


def _model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=embed.DEFAULT_MODEL, help="embedding model, provider/name"
    )
    parser.add_argument("--dimensions", type=int, default=embed.DEFAULT_DIMENSIONS)
    parser.add_argument(
        "--api-base", default=embed.DEFAULT_API_BASE, help="OpenAI-compatible base URL"
    )
    parser.add_argument(
        "--key-env",
        default=embed.DEFAULT_KEY_ENV,
        help="environment variable with the API key",
    )
    parser.add_argument(
        "--paths",
        default="**/*",
        help="glob of files (default: all, .gitignore applies)",
    )


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="pyedit index",
        description=f"A code index: definitions as vectors in the git ref {gitstore.REF}.",
    )
    verbs = top.add_subparsers(dest="verb", required=True)
    built = verbs.add_parser("build", help="embed what the index lacks and save it")
    _model_options(built)
    built.add_argument(
        "-v", "--verbose", action="store_true", help="progress on stderr"
    )
    asked = verbs.add_parser(
        "sql",
        help="query the index in SQLite",
        description=sql.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _model_options(asked)
    asked.add_argument("query", help="SQL; rows of its last statement are printed")
    return top


def _failures() -> tuple[type[Exception], ...]:
    """Expected failures: each ends the run with one line, not a traceback."""
    import apsw

    found: tuple[type[Exception], ...] = (
        embed.EmbedderMissing,
        gitstore.IndexStoreError,
        apsw.Error,
    )
    try:
        from aid_models.errors import AidError
    except ImportError:
        return found
    # a missing key, an outage, a refused request
    return (*found, AidError)


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    session = EditSession(root=Path.cwd())

    def embedder() -> embed.Embedder:
        return embed.hosted(args.model, args.dimensions, args.api_base, args.key_env)

    try:
        if args.verb == "build":
            made = embedder()
            stats = build(session, made, pattern=args.paths, verbose=args.verbose)
            result: object = {
                **asdict(stats),
                "tokens": getattr(made, "tokens", None),
                "cost": getattr(made, "cost", None),
            }
        else:
            gathered = gather(session, args.model, args.dimensions, pattern=args.paths)
            if gathered.stale:
                print(
                    f"pyedit: index: {len(gathered.stale)} files have no vectors "
                    "(new or changed since the build); run `pyedit index build`",
                    file=sys.stderr,
                )
            result = sql.query(sql.connect(session, gathered, embedder), args.query)
    except _failures() as exc:
        print(f"pyedit: index: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    return 0
