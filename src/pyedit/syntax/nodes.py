"""Data shared by the syntax package, plus pure position math.

Positions are pyedit's everywhere: 1-based line, 0-based character
column. Tree-sitter works in (row, byte column) points; the converters
here translate between the two. Stdlib only, so both the engine and
the per-language rules can import this freely.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NodeInfo:
    kind: str
    name: str | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str
    # the nearest enclosing node that names something, set by node_at
    enclosing: "NodeInfo | None" = None


@dataclass
class SyntaxProblem:
    line: int
    column: int
    message: str
    # exclusive 0-based end; None renders a single caret
    end_column: int | None = None


def char_column(line_text: str, byte_column: int) -> int:
    """A tree-sitter byte column as a character column."""
    return len(line_text.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore"))


def byte_offset(lines: list[str], line: int, column: int) -> int:
    """A (1-based line, 0-based char column) position as a byte offset,
    bounds-checked against the file shape."""
    if line < 1 or line > len(lines):
        raise ValueError(f"line {line} is past the end of the file ({len(lines)} lines)")
    line_text = lines[line - 1]
    if column < 0 or column > len(line_text):
        raise ValueError(
            f"column {column} is past the end of line {line} "
            f"({len(line_text)} characters)"
        )
    offset = sum(len(part.encode("utf-8")) + 1 for part in lines[: line - 1])
    return offset + len(line_text[:column].encode("utf-8"))
