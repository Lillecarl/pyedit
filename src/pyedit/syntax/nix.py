"""Nix rules: bindings carry an attrpath instead of a name field, so
titles come from the attrpath text (e.g. `beta.gamma`)."""

from __future__ import annotations

from pyedit.syntax.rules import LanguageRules


class Nix(LanguageRules):
    grammar = "tree_sitter_nix"
    suffixes = (".nix",)

    def title(self, node) -> str | None:
        if node.type != "binding":
            return None
        attrpath = node.child_by_field_name("attrpath")
        if attrpath is None:
            return None
        return attrpath.text.decode("utf-8", errors="replace")
