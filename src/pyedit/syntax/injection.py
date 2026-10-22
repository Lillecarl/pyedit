"""Language injection: a host grammar's query names regions that
belong to another language, and those regions are parsed with that
language's grammar.

The query follows the nvim-treesitter convention: `@injection.content`
captures the region, `(#set! injection.language "NAME")` names the
language. When several patterns capture the same region the last one
wins, so a general pattern can be refined by a later specific one.
The injected parser runs over the whole text with `included_ranges`
set to the region, so every position it reports is already in host
coordinates.
"""

from __future__ import annotations

from pyedit.syntax.nodes import SyntaxProblem

# injection language name -> a suffix whose rules and grammar parse it
_SUFFIX = {
    "javascript": ".js",
    "typescript": ".ts",
    "tsx": ".tsx",
    "css": ".css",
}


def injected_problems(
    text: str, tree, language, query_text: str
) -> list[SyntaxProblem]:
    """Problems inside every region `query_text` injects, parsed by the
    injected language. A language with no grammar installed is skipped."""
    from tree_sitter import Parser, Query, QueryCursor, Range

    from pyedit.syntax import _language_for
    from pyedit.syntax.rules import rules_for

    query = Query(language, query_text)
    regions: dict[tuple[int, int], tuple[int, object, str]] = {}
    for index, captures in QueryCursor(query).matches(tree.root_node):
        name = query.pattern_settings(index).get("injection.language")
        for node in captures.get("injection.content", []):
            key = (node.start_byte, node.end_byte)
            if key not in regions or regions[key][0] < index:
                regions[key] = (index, node, name)

    source = text.encode("utf-8")
    found: list[SyntaxProblem] = []
    for _, node, name in regions.values():
        suffix = _SUFFIX.get(name)
        injected = _language_for(suffix) if suffix else None
        if injected is None:
            continue
        parser = Parser(injected)
        parser.included_ranges = [
            Range(node.start_point, node.end_point, node.start_byte, node.end_byte)
        ]
        found.extend(rules_for(suffix).problems(text, parser.parse(source)))
    found.sort(key=lambda problem: (problem.line, problem.column))
    return found
