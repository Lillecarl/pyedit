"""Nix rules: bindings carry an attrpath instead of a name field, so
titles come from the attrpath text (e.g. `beta.gamma`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.nodes import node_text, override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Node


class Nix(LanguageRules):
    grammar = "tree_sitter_nix"
    suffixes = (".nix",)

    @override
    def title(self, node: Node) -> str | None:
        if node.type != "binding":
            return None
        attrpath = node.child_by_field_name("attrpath")
        if attrpath is None:
            return None
        return node_text(attrpath)
