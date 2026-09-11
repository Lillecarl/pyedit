"""Python rules: compile() is the gold standard, so checking uses it
instead of tree-sitter -- precise messages, no permissive-grammar
false negatives. Position queries still go through the tree-sitter
grammar for span accuracy.
"""

from __future__ import annotations

from pyedit.syntax.nodes import SyntaxProblem
from pyedit.syntax.rules import LanguageRules


class Python(LanguageRules):
    grammar = "tree_sitter_python"
    suffixes = (".py", ".pyi")
    noise = (
        "default_parameter",
        "aliased_import",
        "import_statement",
        "import_from_statement",
    )

    def problems(self, text: str, tree) -> list[SyntaxProblem]:
        del tree  # compile() alone; the grammar would only duplicate it
        try:
            compile(text, "<staged>", "exec")
        except SyntaxError as err:
            return [
                SyntaxProblem(
                    line=err.lineno or 1,
                    column=(err.offset or 1) - 1,
                    message=err.msg,
                )
            ]
        return []
