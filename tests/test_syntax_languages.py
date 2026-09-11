"""Every shipped language, against one matrix: a correct sample must
parse clean and outline its definitions; a broken sample must surface
problems. One row per language -- add a language by adding a row.
"""

import pytest

from pyedit.session import EditSession
from pyedit.syntax import outline, problems

LANGUAGES = [
    # name, suffix, correct sample, broken sample, expected outline names
    (
        "python",
        ".py",
        "def alpha(x, y=1):\n    return x + y\n\nclass Beta:\n    def gamma(self):\n        return 1\n",
        "def broken(:\n    pass\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "javascript",
        ".js",
        "function alpha(x) {\n  return x;\n}\n\nconst noise = 2;\n\nclass Beta {\n  gamma() {\n    return 1;\n  }\n}\n",
        "function broken( {\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "typescript",
        ".ts",
        "function alpha(x: number): number {\n  return x;\n}\n\nconst noise = 2;\n\nclass Beta {\n  gamma(): number {\n    return 1;\n  }\n}\n",
        "function broken( {\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "tsx",
        ".tsx",
        "export function App() {\n  return null;\n}\n\nclass Widget {\n  render() {\n    return null;\n  }\n}\n",
        "function broken( {\n",
        {"App", "Widget", "render"},
    ),
    (
        "go",
        ".go",
        "package main\n\nfunc Alpha(x int) int {\n\treturn x\n}\n\ntype Beta struct {\n\tgamma int\n}\n\nfunc (b Beta) delta() int {\n\treturn b.gamma\n}\n",
        "func broken(:\n",
        {"Alpha", "Beta", "delta"},
    ),
    (
        "rust",
        ".rs",
        "fn alpha(x: u32) -> u32 {\n    x\n}\n\nstruct Beta {\n    gamma: u32,\n}\n\nimpl Beta {\n    fn gamma(&self) -> u32 {\n        self.gamma\n    }\n}\n",
        "fn broken( {\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "c",
        ".c",
        "struct Beta {\n    int gamma;\n};\n\nint alpha(int x) {\n    return x;\n}\n",
        "int broken(:\n",
        {"alpha", "Beta"},
    ),
    (
        "cpp",
        ".cpp",
        "class Beta {\npublic:\n    int gamma() { return 1; }\n};\n\nint alpha(int x) {\n    return x;\n}\n",
        "int broken(:\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "bash",
        ".sh",
        "alpha() {\n  echo hi\n}\n",
        "if true; then\n",
        {"alpha"},
    ),
    ("json", ".json", '{"alpha": 1,\n "beta": [2, 3]\n}\n', '{"a": 1,,}\n', set()),
    ("yaml", ".yaml", "alpha: 1\nbeta:\n  gamma: 2\n", "alpha: [unclosed\n", set()),
    ("toml", ".toml", "alpha = 1\n\n[beta]\ngamma = 2\n", "[broken\n", set()),
    ("nix", ".nix", "{\n  alpha = 1;\n  beta.gamma = 2;\n}\n", "{ alpha = ; }\n", {"alpha", "beta.gamma"}),
    (
        "ruby",
        ".rb",
        "def alpha\n  1\nend\n\nclass Beta\n  def gamma\n    2\n  end\nend\n",
        "def broken(:\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "java",
        ".java",
        "class Beta {\n    int alpha() { return 1; }\n    void gamma() {}\n}\n",
        "class {\n",
        {"alpha", "Beta", "gamma"},
    ),
    (
        "lua",
        ".lua",
        "function alpha()\n    return 1\nend\n",
        "function (:\n",
        {"alpha"},
    ),
    (
        "zig",
        ".zig",
        "fn alpha() void {\n}\n",
        "fn broken(:\n",
        {"alpha"},
    ),
]


def test_table_covers_every_shipped_suffix():
    from pyedit.syntax.rules import RULES

    covered = {id(RULES[suffix]) for _, suffix, *_ in LANGUAGES}
    assert {id(rules) for rules in RULES.values()} == covered


@pytest.mark.parametrize(
    "name,suffix,correct,broken,names", LANGUAGES, ids=[entry[0] for entry in LANGUAGES]
)
def test_language_correct_and_incorrect(name, suffix, correct, broken, names, tmp_path):
    session = EditSession(respect_gitignore=False, root=tmp_path)
    path = "sample" + suffix

    session.write(path, correct)
    assert problems(session, path) == [], f"{name}: clean sample flagged"
    entries = outline(session, path)
    assert {entry.name for entry in entries} == names, f"{name}: outline mismatch"

    # the cursor on a definition names it, directly or through its
    # enclosing node (bash's word node has no title, its function does)
    first = entries[0] if entries else None
    if first is not None:
        from pyedit.syntax import node_at

        info = node_at(session, path, first.start_line, first.start_column)
        titled = info.name or (info.enclosing.name if info.enclosing else None)
        assert titled == first.name, f"{name}: node_at missed {first.name}"

    session.write(path, broken)
    found = problems(session, path)
    assert found, f"{name}: broken sample not flagged"
    assert all(p.line >= 1 for p in found)
