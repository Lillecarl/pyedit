"""C family rules: function names hide inside the declarator chain
(`int alpha(int)` is function_definition -> function_declarator ->
identifier), so titles dig them out. Structs, enums and classes title
from their name field like everywhere else.

A `.h` file reads as C++: the C++ grammar takes C headers too, and the
C grammar rejects most of a C++ one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import node_text, override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node


class C(LanguageRules):
    grammar = "tree_sitter_c"
    suffixes = (".c",)
    noise = ("field_declaration", "parameter_declaration")

    @override
    def title(self, node: Node) -> str | None:
        if node.type == "function_definition":
            return _declarator_name(node)
        return super().title(node)


class Cpp(C):
    grammar = "tree_sitter_cpp"
    suffixes = (".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx")
    noise = (
        *C.noise,
        "qualified_identifier",
        "template_function",
        "template_method",
        "template_type",
    )


_FUNCTION_NAMES = {
    "destructor_name",
    "field_identifier",
    "identifier",
    "operator_cast",
    "operator_name",
    "qualified_identifier",
    "template_function",
    "type_identifier",
}


def _declarator_name(node: Node) -> str | None:
    declarator = node.child_by_field_name("declarator")
    for _ in range(8):
        if declarator is None or declarator.type in _FUNCTION_NAMES:
            break
        inner = declarator.child_by_field_name("declarator")
        if inner is None and declarator.type == "reference_declarator":
            # `T& f()`: the grammar gives this child no field
            inner = declarator.named_children[-1]
        declarator = inner
    if declarator is None or declarator.type not in _FUNCTION_NAMES:
        return None
    return node_text(declarator)
