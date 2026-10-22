"""Svelte rules: `<script>` and `<style>` bodies are other languages,
so they are checked with the JavaScript, TypeScript and CSS grammars.

The grammar ships these as raw text and an injections query for the
host to apply; this is a trimmed copy of that query, limited to the
languages pyedit has grammars for."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyedit.syntax.injection import injected_problems
from pyedit.syntax.nodes import override
from pyedit.syntax.rules import LanguageRules

if TYPE_CHECKING:
    from tree_sitter import Tree

    from pyedit.syntax.nodes import SyntaxProblem

_INJECTIONS = """
((script_element (raw_text) @injection.content)
  (#set! injection.language "javascript"))

((script_element
   (start_tag
     (attribute
       (attribute_name) @_attr
       (quoted_attribute_value (attribute_value) @_lang)))
   (raw_text) @injection.content)
  (#eq? @_attr "lang")
  (#any-of? @_lang "ts" "typescript")
  (#set! injection.language "typescript"))

((style_element (raw_text) @injection.content)
  (#set! injection.language "css"))
"""


class Svelte(LanguageRules):
    grammar = "tree_sitter_svelte"
    suffixes = (".svelte",)

    @override
    def problems(self, text: str, tree: Tree | None) -> list[SyntaxProblem]:
        if tree is None:
            return []
        found = super().problems(text, tree)
        found += injected_problems(text, tree, tree.language, _INJECTIONS)
        found.sort(key=lambda problem: (problem.line, problem.column))
        return found
