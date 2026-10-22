"""Markdown rules: a heading is titled by its inline content."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import node_text, override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node


class Markdown(LanguageRules):
    grammar = "tree_sitter_markdown"
    suffixes = (".md", ".markdown")
    noise = ()

    @override
    def title(self, node: Node) -> str | None:
        if node.type in ("atx_heading", "setext_heading"):
            stack = [node]
            while stack:
                current = stack.pop()
                if current.type == "inline":
                    return node_text(current)
                stack.extend(current.children)
            return None
        return super().title(node)
