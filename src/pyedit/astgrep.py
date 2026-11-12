"""Code search and rewrite by pattern, through ast-grep (ast-grep-py).

A pattern is code with metavariables: `foo($A, $$$REST)`. ast-grep
matches it against the tree-sitter tree, so whitespace and comments
do not matter. `rule` takes ast-grep's rule object as a dict (`kind`,
`inside`, `has`, `not`, ...), for what a pattern alone cannot say.

The binding takes source text and returns text: it never opens a
file, so it sees staged content and every result is staged through
the session. Never shell out to `sg` instead -- a subprocess reads
disk and bypasses the overlay.

Traps, measured against ast-grep-py 0.50.0:
- an unknown language name panics, and PanicException derives from
  BaseException, which the runner does not catch; the language is
  checked against SUFFIX_LANGUAGES first, and every call is wrapped
- a pattern that does not parse (`foo(`) matches nothing, silently
- `$$$NAME` captures separators too (`a`, `,`, `b`): its value here
  is the source span from the first node to the last
- `commit_edits` drops an edit nested in another without a word, so
  overlaps are resolved here and the count stays true
- positions are 0-based lines and CHARACTER columns; CodeMatch
  converts to pyedit's 1-based lines and UTF-8 byte columns
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ast_grep_py import SgNode

    from pyedit.session import EditSession

# ast-grep's built-in grammars, by file suffix. Not here (no grammar
# in ast-grep): fish, zsh, toml, svelte -- pyedit.query covers them.
SUFFIX_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".sh": "bash",
    ".bash": "bash",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".nix": "nix",
    ".rb": "ruby",
    ".java": "java",
    ".lua": "lua",
    ".zig": "zig",
    ".css": "css",
    ".md": "markdown",
    ".html": "html",
    ".htm": "html",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
    ".ex": "elixir",
    ".exs": "elixir",
    ".hs": "haskell",
    ".hcl": "hcl",
    ".tf": "hcl",
    ".sol": "solidity",
}
LANGUAGES = frozenset(SUFFIX_LANGUAGES.values())

_GLOB_CHARS = frozenset("*?[")
_METAVAR = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")


class PatternError(ValueError):
    """ast-grep rejected a pattern, a rule or a language."""


@dataclass
class CodeMatch:
    # relative to the session root; absolute outside it
    path: str
    kind: str
    text: str
    # splice-ready: 1-based lines, 0-based utf-8 BYTE columns
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    # metavariable -> matched source; `$$$NAME` maps to the whole span
    vars: dict[str, str] = field(default_factory=dict)
    # character offsets into the file's text, for slicing
    start_index: int = 0
    end_index: int = 0


def find_code(
    session: EditSession,
    paths: str | Path,
    pattern: str | None = None,
    *,
    rule: dict[str, Any] | None = None,
    lang: str | None = None,
) -> list[CodeMatch]:
    """Every match in a file or a glob, outermost first per file."""
    config = _config(pattern, rule)
    return [
        match
        for path, text, language in _sources(session, paths, lang)
        for match in _matches(session.root, path, text, language, config)
    ]


def rewrite(
    session: EditSession,
    paths: str | Path,
    pattern: str | None,
    fix: str | Callable[[CodeMatch], str],
    *,
    rule: dict[str, Any] | None = None,
    lang: str | None = None,
    count: int = -1,
) -> int:
    """Replace each match with `fix` and stage the result; return
    the replacement count. Zero matches across all paths raise."""
    config = _config(pattern, rule)
    total = 0
    for path, text, language in _sources(session, paths, lang):
        kept: list[CodeMatch] = []
        for match in _matches(session.root, path, text, language, config):
            # a match inside a kept one is the same code again
            if kept and match.start_index < kept[-1].end_index:
                continue
            kept.append(match)
            if 0 <= count == len(kept):
                break
        if not kept:
            continue
        pieces: list[str] = []
        cursor = 0
        for match in kept:
            pieces.append(text[cursor : match.start_index])
            pieces.append(fix(match) if callable(fix) else expand(fix, match.vars))
            cursor = match.end_index
        pieces.append(text[cursor:])
        session.write(path, "".join(pieces))
        total += len(kept)
    if total == 0:
        raise ValueError(
            f"rewrite: no match for {pattern!r} in {paths}"
            + (f" with rule {rule!r}" if rule else "")
            + "; a pattern that does not parse as the file's language "
            "matches nothing too"
        )
    return total


def expand(template: str, values: dict[str, str]) -> str:
    """Fill `$NAME` and `$$$NAME` from a match. A name the match does
    not bind stays as written: `$HOME` in shell is not a metavariable."""

    def one(found: re.Match[str]) -> str:
        name = found.group(1) or found.group(2)
        return values.get(name, found.group(0))

    return _METAVAR.sub(one, template)


def language_for(path: str | Path, lang: str | None = None) -> str:
    if lang is not None:
        if lang not in LANGUAGES:
            raise PatternError(
                f"ast-grep has no language {lang!r}; known: {', '.join(sorted(LANGUAGES))}"
            )
        return lang
    suffix = Path(path).suffix.lower()
    found = SUFFIX_LANGUAGES.get(suffix)
    if found is None:
        raise PatternError(
            f"{path}: ast-grep has no grammar for {suffix or 'a file without suffix'!r}; "
            "pass lang=, or use pyedit.query (tree-sitter) for this file"
        )
    return found


def _config(pattern: str | None, rule: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(rule or {})
    if pattern is not None:
        merged["pattern"] = pattern
    if not merged:
        raise PatternError("give a pattern, a rule, or both")
    return {"rule": merged}


def _sources(
    session: EditSession, paths: str | Path, lang: str | None
) -> Iterator[tuple[Path, str, str]]:
    if _GLOB_CHARS & set(str(paths)):
        for path in session.glob(str(paths)):
            text = session.read(path)
            if not isinstance(text, str):
                continue
            # a glob over mixed files skips what ast-grep cannot parse
            if lang is None and path.suffix.lower() not in SUFFIX_LANGUAGES:
                continue
            yield path, text, language_for(path, lang)
        return
    text = session.read(paths)
    if not isinstance(text, str):
        raise ValueError(f"{session.canon(paths)} is binary; ast-grep works on text")
    yield session.canon(paths), text, language_for(paths, lang)


def _matches(
    root: Path, path: Path, text: str, language: str, config: dict[str, Any]
) -> list[CodeMatch]:
    from ast_grep_py import SgRoot

    names = sorted(set(_names(config)))
    shown = (
        path.relative_to(root).as_posix()
        if path.is_relative_to(root)
        else path.as_posix()
    )
    try:
        found = SgRoot(text, language).root().find_all(config)  # pyrefly: ignore[bad-argument-type]
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as err:
        raise PatternError(
            f"{shown}: ast-grep rejected {config['rule']!r}: {err}"
        ) from None
    lines = text.split("\n")
    out = [_record(shown, node, text, lines, names) for node in found]
    out.sort(key=lambda m: (m.start_index, -m.end_index))
    return out


def _record(
    path: str, node: SgNode, text: str, lines: list[str], names: list[str]
) -> CodeMatch:
    span = node.range()
    values: dict[str, str] = {}
    for name in names:
        single = node.get_match(name)
        if single is not None:
            values[name] = single.text()
            continue
        multi = node.get_multiple_matches(name)
        if multi:
            values[name] = text[
                multi[0].range().start.index : multi[-1].range().end.index
            ]
        else:
            values[name] = ""
    return CodeMatch(
        path=path,
        kind=node.kind(),
        text=node.text(),
        start_line=span.start.line + 1,
        start_column=_byte_column(lines, span.start.line, span.start.column),
        end_line=span.end.line + 1,
        end_column=_byte_column(lines, span.end.line, span.end.column),
        vars=values,
        start_index=span.start.index,
        end_index=span.end.index,
    )


def _byte_column(lines: list[str], line: int, column: int) -> int:
    return len(lines[line][:column].encode("utf-8")) if line < len(lines) else column


def _names(value: object) -> Iterator[str]:
    """Metavariable names in every pattern string of a rule."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "pattern" and isinstance(inner, str):
                for found in _METAVAR.finditer(inner):
                    name = found.group(1) or found.group(2)
                    if name != "_":
                        yield name
            else:
                yield from _names(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _names(inner)
