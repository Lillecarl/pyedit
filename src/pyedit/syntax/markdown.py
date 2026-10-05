"""Markdown rules: a heading is titled by its inline content."""

from __future__ import annotations

from pyedit.syntax.rules import LanguageRules


class Markdown(LanguageRules):
    grammar = "tree_sitter_markdown"
    suffixes = (".md", ".markdown")
    noise = ()

    def title(self, node) -> str | None:
        if node.type in ("atx_heading", "setext_heading"):
            stack = [node]
            while stack:
                current = stack.pop()
                if current.type == "inline":
                    return current.text.decode("utf-8", errors="replace")
                stack.extend(current.children)
            return None
        return super().title(node)
