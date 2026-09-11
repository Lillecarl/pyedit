"""Per-language rules for the syntax package.

Every language gets a `LanguageRules` instance; languages with
behavior beyond the generic tree-sitter defaults subclass it in their
own module (python.py, nix.py). A missing grammar degrades: checking
returns nothing, position queries raise in the engine.

The default `title` reads the grammar's `name` field, and the default
`problems` walks the tree for outermost ERROR and missing nodes.
`noise` lists node kinds that are structure, not outline entries.
"""

from __future__ import annotations

from pyedit.syntax.nodes import SyntaxProblem, char_column

# leaf node kinds that ARE their own name; grammars hang a `name` field
# on the definition node, the identifier under it has none
_SELF_NAMED = {
    "identifier",
    "property_identifier",
    "field_identifier",
    "shorthand_property_identifier",
    "type_identifier",
    "symbol",
}


class LanguageRules:
    grammar: str = ""
    suffixes: tuple[str, ...] = ()
    noise: tuple[str, ...] = ()

    def __init__(
        self,
        grammar: str = "",
        suffixes: tuple[str, ...] = (),
        noise: tuple[str, ...] = (),
    ) -> None:
        if grammar:
            self.grammar = grammar
        if suffixes:
            self.suffixes = suffixes
        if noise:
            self.noise = noise

    def title(self, node) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return name_node.text.decode("utf-8", errors="replace")

    def leaf_title(self, node) -> str | None:
        """The name for whatever the cursor landed on: definitions carry
        a name field; a bare identifier is its own name."""
        titled = self.title(node)
        if titled is not None:
            return titled
        if node.type in _SELF_NAMED and not any(child.is_named for child in node.children):
            return node.text.decode("utf-8", errors="replace")
        return None

    def problems(self, text: str, tree) -> list[SyntaxProblem]:
        """Outermost tree-sitter errors in pyedit positions. `tree` is
        None when no grammar loaded; there is nothing to report then."""
        if tree is None:
            return []
        found: list[SyntaxProblem] = []
        lines = text.split("\n")
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                row, byte_column = node.start_point
                line_text = lines[row] if row < len(lines) else ""
                found.append(
                    SyntaxProblem(
                        line=row + 1,
                        column=char_column(line_text, byte_column),
                        message=(
                            "syntax error" if not node.is_missing else f"missing {node.type}"
                        ),
                    )
                )
                continue  # outermost only: children of an ERROR add nothing
            stack.extend(node.children)
        found.sort(key=lambda problem: (problem.line, problem.column))
        return found


_PLAIN = [
    ("tree_sitter_javascript", (".js", ".jsx", ".mjs", ".cjs"), ("variable_declarator", "command")),
    ("tree_sitter_typescript", (".ts", ".mts", ".cts"), ("variable_declarator",)),
    ("tree_sitter_tsx", (".tsx",), ("variable_declarator",)),
    ("tree_sitter_go", (".go",), ("parameter_declaration", "field_declaration")),
    ("tree_sitter_rust", (".rs",), ("field_declaration",)),
    ("tree_sitter_bash", (".sh", ".bash"), ("command",)),
    ("tree_sitter_json", (".json",), ()),
    ("tree_sitter_yaml", (".yaml", ".yml"), ()),
    ("tree_sitter_toml", (".toml",), ()),
    ("tree_sitter_ruby", (".rb",), ()),
    ("tree_sitter_java", (".java",), ()),
    ("tree_sitter_lua", (".lua",), ()),
    ("tree_sitter_zig", (".zig",), ()),
]


def _rules_by_suffix() -> dict[str, LanguageRules]:
    from pyedit.syntax.c import C, Cpp
    from pyedit.syntax.nix import Nix
    from pyedit.syntax.python import Python

    special = (*_PLAIN, C, Cpp, Python, Nix)
    table: dict[str, LanguageRules] = {}
    for entry in special:
        rules = entry() if isinstance(entry, type) else LanguageRules(*entry)
        for suffix in rules.suffixes:
            table[suffix] = rules
    return table


RULES: dict[str, LanguageRules] = _rules_by_suffix()


def rules_for(suffix: str) -> LanguageRules | None:
    return RULES.get(suffix)
