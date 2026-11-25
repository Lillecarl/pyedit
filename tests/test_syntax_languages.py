"""The per-language syntax matrix, library level: a correct sample
must parse clean and outline exactly its definitions; a broken sample
must surface problems. Corpus lives in syntax_corpus.py, the CLI-level
gate for the same rows lives in test_cli.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from syntax_corpus import LANGUAGES

from pyedit.session import EditSession
from pyedit.syntax import node_at, outline, problems
from pyedit.syntax.rules import RULES

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("suffix", [".ts", ".tsx"])
@pytest.mark.parametrize(
    "source",
    [
        'export type * from "./mod";\n',
        'export type * as ns from "./mod";\n',
    ],
)
def test_typescript5_export_type_star(suffix: str, source: str, tmp_path: Path) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write("sample" + suffix, source)
    assert problems(session, "sample" + suffix) == []


SVELTE_TS = '<script lang="ts">\n{body}\n</script>\n<h1>hi</h1>\n'


@pytest.mark.parametrize(
    "source,flagged_line",
    [
        (SVELTE_TS.format(body='export type * from "./m";\nlet x: number = 1;'), None),
        (SVELTE_TS.format(body="let x: = ;"), 2),
        ("<script>\nlet x = ;\n</script>\n", 2),
        ("<style>\nh1 { color: ; }\nh2 {{{\n</style>\n", 3),
    ],
    ids=["ts-valid", "ts-broken", "js-broken", "css-broken"],
)
def test_svelte_embedded_languages(
    source: str, flagged_line: int | None, tmp_path: Path
) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write("sample.svelte", source)
    found = problems(session, "sample.svelte")
    assert [p.line for p in found][:1] == (
        [] if flagged_line is None else [flagged_line]
    )


CPP_FUNCTIONS = """\
namespace desk {
Window::Window() {}
Window::~Window() {}
const Page& Window::page() const { return page_; }
Page* Window::Find(int id) { return nullptr; }
bool operator==(const Page& a, const Page& b) { return true; }
Page& Get() { return Window::instance().page(); }
}
"""


@pytest.mark.parametrize("suffix", [".cc", ".h"])
def test_cpp_functions_defined_outside_their_class(suffix: str, tmp_path: Path) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write("sample" + suffix, CPP_FUNCTIONS)
    assert problems(session, "sample" + suffix) == []
    assert [entry.name for entry in outline(session, "sample" + suffix)] == [
        "desk",
        "Window::Window",
        "Window::~Window",
        "Window::page",
        "Window::Find",
        "operator==",
        "Get",
    ]


def test_a_cpp_header_parses_clean(tmp_path: Path) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write(
        "sample.h",
        "class Desk final : public Base {\n public:\n  int a() override;\n};\n",
    )
    assert problems(session, "sample.h") == []


def test_rust_paths_and_struct_literals_are_noise(tmp_path: Path) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write(
        "sample.rs",
        "use std::time::Duration;\n\nfn alpha() -> Beta {\n"
        "    Beta {\n        gamma: std::cmp::max(1, 2),\n    }\n}\n",
    )
    assert [entry.name for entry in outline(session, "sample.rs")] == ["alpha"]


@pytest.mark.parametrize("suffix", [".js", ".ts", ".tsx"])
def test_a_function_in_a_constant_is_a_definition(suffix: str, tmp_path: Path) -> None:
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write(
        "sample" + suffix,
        "export const alpha = (x) => x + 1;\n"
        "const beta = function () {\n  return 1;\n};\n"
        "const noise = alpha(2);\n",
    )
    assert [entry.name for entry in outline(session, "sample" + suffix)] == [
        "alpha",
        "beta",
    ]


def test_table_covers_every_shipped_suffix() -> None:
    covered = {id(RULES[suffix]) for _, suffix, *_ in LANGUAGES}
    assert {id(rules) for rules in RULES.values()} == covered


@pytest.mark.parametrize(
    "name,suffix,correct,broken,names", LANGUAGES, ids=[entry[0] for entry in LANGUAGES]
)
def test_language_correct_and_incorrect(
    name: str, suffix: str, correct: str, broken: str, names: set[str], tmp_path: Path
) -> None:
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
        info = node_at(session, path, first.start_line, first.start_column)
        titled = info.name or (info.enclosing.name if info.enclosing else None)
        assert titled == first.name, f"{name}: node_at missed {first.name}"

    session.write(path, broken)
    found = problems(session, path)
    assert found, f"{name}: broken sample not flagged"
    assert all(p.line >= 1 and p.column >= 0 for p in found)
    assert all(p.message for p in found)
