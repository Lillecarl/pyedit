"""Syntax awareness: parse files, answer position questions, report
syntax problems.

Parsing goes through tree-sitter when a grammar for the file's suffix
is installed; grammars are optional, so a language without one simply
degrades -- checking skips it, position queries raise. Python checks
run through compile() instead (see python.py). Positions are pyedit's
everywhere: 1-based line, 0-based character column.
"""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path

from pyedit.diff import display_path
from pyedit.syntax.nodes import NodeInfo, SyntaxProblem, byte_offset, char_column
from pyedit.syntax.rules import RULES, rules_for

__all__ = [
    "NodeInfo",
    "SyntaxProblem",
    "known_language",
    "node_at",
    "outline",
    "problems",
    "render",
]

_parsers: dict[str, object | None] = {}


def _parser_for(suffix: str):
    """The parser for a suffix, or None when no grammar is available."""
    if suffix in _parsers:
        return _parsers[suffix]
    rules = rules_for(suffix)
    if rules is None:
        _parsers[suffix] = None
        return None
    try:
        module = importlib.import_module(rules.grammar)
        from tree_sitter import Language, Parser

        with warnings.catch_warnings():
            # the generated bindings hand back an int pointer; Language(int)
            # is deprecated against the capsule API but works
            warnings.simplefilter("ignore", DeprecationWarning)
            parser = Parser(Language(module.language()))
    except ImportError:
        _parsers[suffix] = None
        return None
    _parsers[suffix] = parser
    return parser


def known_language(suffix: str) -> bool:
    return rules_for(suffix) is not None


def _no_grammar(suffix: str) -> ValueError:
    return ValueError(
        f"no tree-sitter grammar for {suffix or 'this suffix'} files; "
        f"supported suffixes: {', '.join(sorted(RULES))}"
    )


def _read_text(session, path: str | Path) -> str:
    content = session.read(path)
    if not isinstance(content, str):
        raise ValueError(f"{session.canon(path)} is binary; syntax queries work on text")
    return content


def _info(node, lines: list[str], rules) -> NodeInfo:
    start_row, start_byte = node.start_point
    end_row, end_byte = node.end_point
    name_node = node.child_by_field_name("name")
    name_row, name_byte = (
        (name_node.start_point if name_node is not None else (None, None))
    )
    return NodeInfo(
        kind=node.type,
        name=rules.title(node),
        start_line=start_row + 1,
        start_column=char_column(lines[start_row], start_byte),
        end_line=end_row + 1,
        end_column=char_column(lines[end_row], end_byte),
        text=node.text.decode("utf-8", errors="replace"),
        name_line=None if name_row is None else name_row + 1,
        name_column=None if name_byte is None else char_column(
            lines[name_row], name_byte
        ),
    )


def _smallest_containing(root, offset: int):
    """The smallest named node whose span covers the byte offset.

    Spans are half-open, tree-sitter style: a cursor sitting exactly at
    a node's end belongs to what follows, not to it.
    """
    best = None
    stack = [root]
    while stack:
        node = stack.pop()
        if not node.is_named:
            continue
        if node.start_byte > offset or node.end_byte <= offset:
            continue
        best = node
        stack.extend(node.children)
    return best


def free_names(text: str) -> set[str]:
    """Names the module text uses but does not bind."""
    import ast

    names: set[str] = set()
    bound: set[str] = set()

    def visit(n):
        if isinstance(n, ast.Name):
            (bound if isinstance(n.ctx, (ast.Store, ast.Del)) else names).add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            names.add(n.value.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Import):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(ast.parse(text))
    import builtins

    return names - bound - {
        n for n in dir(builtins) if not n.startswith("_")
    }


def locate_definition(session, path: str | Path, name: str) -> tuple[int, int]:
    """The (line, column) of the identifier that defines `name`.

    Raises ValueError when the file has no definition or several,
    listing the candidates; a caller can then pass a position."""
    matches = [
        info
        for info in outline(session, path)
        if info.name == name and info.name_column is not None
    ]
    display = display_path(session.canon(path))
    if len(matches) == 1:
        return matches[0].name_line, matches[0].name_column
    if not matches:
        raise ValueError(f"no definition of {name!r} in {display}")
    listed = "; ".join(
        f"{info.name} at line {info.name_line}, column {info.name_column}"
        for info in matches
    )
    raise ValueError(
        f"{name!r} is defined {len(matches)} times in {display}: "
        f"{listed} -- pass line and column"
    )


def node_at(session, path: str | Path, line: int, column: int) -> NodeInfo:
    """The smallest syntax node at a position, with its named parent.

    The cursor may sit anywhere inside the node, including just after
    its last character. `enclosing` is the nearest ancestor that names
    something -- for a cursor inside a method, the method definition.
    """
    suffix = Path(path).suffix
    rules = rules_for(suffix)
    if rules is None or _parser_for(suffix) is None:
        raise _no_grammar(suffix)
    text = _read_text(session, path)
    lines = text.split("\n")
    offset = byte_offset(lines, line, column)
    tree = _parser_for(suffix).parse(text.encode("utf-8"))
    node = _smallest_containing(tree.root_node, offset)
    if node is None and offset > 0:
        node = _smallest_containing(tree.root_node, offset - 1)
    else:
        # a cursor just after a token snaps back into it, like editors do:
        # prefer a smaller node ending exactly at the cursor when the node
        # at the cursor merely contains it
        after = _smallest_containing(tree.root_node, offset - 1) if offset > 0 else None
        if (
            after is not None
            and after is not node
            and after.end_byte == offset
            and node.start_byte <= after.start_byte
            and node.end_byte >= after.end_byte
        ):
            node = after
    if node is None:
        raise ValueError(f"no node at {line}:{column}")
    info = _info(node, lines, rules)
    info.name = rules.leaf_title(node)
    parent = node.parent
    while parent is not None:
        if rules.title(parent) is not None:
            info.enclosing = _info(parent, lines, rules)
            break
        parent = parent.parent
    return info


def outline(session, path: str | Path) -> list[NodeInfo]:
    """Every named definition in the file, ordered by position.

    The generic rule is a node the language rules can title (a name
    field, or the attrpath for nix); kinds listed as noise (parameters,
    struct fields, imports, plain assignments) are excluded.
    """
    suffix = Path(path).suffix
    rules = rules_for(suffix)
    if rules is None or _parser_for(suffix) is None:
        raise _no_grammar(suffix)
    text = _read_text(session, path)
    lines = text.split("\n")
    tree = _parser_for(suffix).parse(text.encode("utf-8"))
    result = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if rules.title(node) is not None and not node.type.startswith(rules.noise):
            result.append(_info(node, lines, rules))
        stack.extend(node.children)
    result.sort(key=lambda entry: (entry.start_line, entry.start_column))
    return result


def render(problem: SyntaxProblem, text: str) -> str:
    """A CPython-style view of one problem: the source line with
    a caret (or span) under the position."""
    lines = text.split("\n")
    line_no = max(1, min(problem.line, len(lines)))
    source = lines[line_no - 1]
    gutter = f"{line_no:>4} | "
    pad = " " * max(0, problem.column)
    width = max(1, (problem.end_column or problem.column) - problem.column)
    return f"{gutter}{source}\n{' ' * len(gutter)}| {pad}{'^' * width}"


def problems(session, path: str | Path) -> list[SyntaxProblem]:
    """Syntax problems in one file, in pyedit positions."""
    content = session.read(path)
    if not isinstance(content, str):
        return []
    suffix = Path(path).suffix
    rules = rules_for(suffix)
    if rules is None:
        return []
    tree = None
    if _parser_for(suffix) is not None:
        tree = _parser_for(suffix).parse(content.encode("utf-8"))
    return rules.problems(content, tree)
