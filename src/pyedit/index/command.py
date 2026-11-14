"""`pyedit index`: build, search, duplicates. Results are JSON on
stdout; warnings and progress go to stderr."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pyedit.index import build, embed, gather, gitstore, similar
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
        help="glob of files to index (default: all, .gitignore applies)",
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
    found = verbs.add_parser("search", help="definitions nearest a description")
    _model_options(found)
    found.add_argument("query")
    found.add_argument("-k", type=int, default=10, help="how many (default 10)")
    copies = verbs.add_parser("duplicates", help="pairs of similar definitions")
    _model_options(copies)
    copies.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="cosine at least this (default 0.9)",
    )
    copies.add_argument(
        "--limit", type=int, default=200, help="at most this many pairs (0: all)"
    )
    return top


def _row(row: similar.Row) -> dict[str, int | str | None]:
    return asdict(row)


def _embedder(args: argparse.Namespace) -> embed.Embedder:
    return embed.hosted(args.model, args.dimensions, args.api_base, args.key_env)


def _failures() -> tuple[type[Exception], ...]:
    """Expected failures: each ends the run with one line, not a traceback."""
    found: tuple[type[Exception], ...] = (
        embed.EmbedderMissing,
        gitstore.IndexStoreError,
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
    try:
        if args.verb == "build":
            embedder = _embedder(args)
            stats = build(session, embedder, pattern=args.paths, verbose=args.verbose)
            result: object = {
                **asdict(stats),
                "tokens": getattr(embedder, "tokens", None),
                "cost": getattr(embedder, "cost", None),
            }
        else:
            gathered = gather(session, args.model, args.dimensions, pattern=args.paths)
            if gathered.stale:
                print(
                    f"pyedit: index: {len(gathered.stale)} files have no vectors "
                    "(new or changed since the build); run `pyedit index build`",
                    file=sys.stderr,
                )
            if args.verb == "search":
                (query,) = _embedder(args).embed([embed.QUERY_PREFIX + args.query])
                result = [
                    {"score": round(score, 4), **_row(row)}
                    for score, row in similar.search(gathered.matrix, query, args.k)
                ]
            else:
                pairs = similar.duplicates(gathered.matrix, args.threshold)
                if args.limit > 0:
                    pairs = pairs[: args.limit]
                result = [
                    {"score": round(score, 4), "a": _row(a), "b": _row(b)}
                    for score, a, b in pairs
                ]
    except _failures() as exc:
        print(f"pyedit: index: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    return 0
