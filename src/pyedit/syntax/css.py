"""CSS rules: a rule set is titled by its selectors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import node_text, override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node


class Css(LanguageRules):
    grammar = "tree_sitter_css"
    suffixes = (".css",)
    noise = ()

    @override
    def title(self, node: Node) -> str | None:
        if node.type == "rule_set":
            for child in node.children:
                if child.is_named and child.type == "selectors":
                    return node_text(child)
            return None
        return super().title(node)
