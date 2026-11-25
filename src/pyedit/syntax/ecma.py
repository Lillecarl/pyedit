"""JavaScript and TypeScript rules: `const f = () => {}` is a function
definition, and every other variable is noise."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node

_FUNCTIONS = {"arrow_function", "function_expression", "generator_function"}


class JavaScript(LanguageRules):
    grammar = "tree_sitter_javascript"
    suffixes = (".js", ".jsx", ".mjs", ".cjs")
    noise = ("command", "jsx_")

    @override
    def title(self, node: Node) -> str | None:
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is None or value.type not in _FUNCTIONS:
                return None
        return super().title(node)


class TypeScript(JavaScript):
    grammar = "tree_sitter_typescript"
    suffixes = (".ts", ".mts", ".cts")
    noise = ("generic_type", "jsx_", "type_parameter")


class Tsx(TypeScript):
    grammar = "tree_sitter_tsx"
    suffixes = (".tsx",)
