"""CSS rules: a rule set is titled by its selectors."""

from __future__ import annotations

from pyedit.syntax.rules import LanguageRules


class Css(LanguageRules):
    grammar = "tree_sitter_css"
    suffixes = (".css",)
    noise = ()

    def title(self, node) -> str | None:
        if node.type == "rule_set":
            for child in node.children:
                if child.is_named and child.type == "selectors":
                    return child.text.decode("utf-8", errors="replace")
            return None
        return super().title(node)
