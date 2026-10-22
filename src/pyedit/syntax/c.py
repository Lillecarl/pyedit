"""C family rules: function names hide inside the declarator chain
(`int alpha(int)` is function_definition -> function_declarator ->
identifier), so titles dig them out. Structs, enums and classes title
from their name field like everywhere else."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import node_text, override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node


class C(LanguageRules):
    grammar = "tree_sitter_c"
    suffixes = (".c", ".h")
    noise = ("field_declaration", "parameter_declaration")

    @override
    def title(self, node: Node) -> str | None:
        if node.type == "function_definition":
            return _declarator_name(node)
        return super().title(node)


class Cpp(C):
    grammar = "tree_sitter_cpp"
    suffixes = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")


def _declarator_name(node: Node) -> str | None:
    declarator = node.child_by_field_name("declarator")
    seen = 0
    while declarator is not None and seen < 8:
        inner = declarator.child_by_field_name("declarator")
        if inner is None:
            break
        declarator = inner
        seen += 1
    if declarator is None or declarator.type not in (
        "identifier",
        "field_identifier",
        "type_identifier",
    ):
        return None
    return node_text(declarator)
