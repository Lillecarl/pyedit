"""Tree-sitter queries: S-expression hits feed splice; kinds names the vocabulary.

Samples live in src/pyedit/corpus/<language>/ (seeded from the syntax
corpus); patterns pin the query spelling per language here.
"""

from pathlib import Path

import pytest

import pyedit as pyedit_pkg
from pyedit.session import EditSession
from pyedit.syntax import kinds, query

CORPUS = Path(pyedit_pkg.__file__).resolve().parent / "corpus"


def sample_text(language):
    [path] = (CORPUS / language).glob("sample.*")
    return path.name, path.read_text()


@pytest.fixture
def session(tmp_path):
    return EditSession(respect_gitignore=False, root=tmp_path)


def test_nix_value_hits_feed_splice(session):
    name, text = sample_text("nix")
    session.write(name, text)
    values = [
        hit
        for hit in query(
            session, name, "(binding (attrpath) @a (integer_expression) @v)"
        )
        if hit.capture == "v"
    ]
    assert [hit.text for hit in values] == ["1", "2"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "9")
        for hit in values
    ]
    assert session.splice(name, spans) == 2
    assert "alpha = 9;" in session.read(name)
    assert "beta.gamma = 9;" in session.read(name)


def test_python_function_names(session):
    name, text = sample_text("python")
    session.write(name, text)
    names = query(session, name, "(function_definition name: (identifier) @n)")
    assert sorted(hit.text for hit in names) == ["alpha", "gamma"]


def test_javascript_function_names_feed_splice(session):
    name, text = sample_text("javascript")
    session.write(name, text)
    names = query(session, name, "(function_declaration name: (identifier) @n)")
    assert [hit.text for hit in names] == ["alpha"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "omega")
        for hit in names
    ]
    assert session.splice(name, spans) == 1
    assert "function omega(x)" in session.read(name)


def test_typescript_method_names(session):
    name, text = sample_text("typescript")
    session.write(name, text)
    names = query(session, name, "(method_definition name: (property_identifier) @n)")
    assert [hit.text for hit in names] == ["gamma"]


def test_go_function_and_method_names(session):
    name, text = sample_text("go")
    session.write(name, text)
    funcs = query(session, name, "(function_declaration name: (identifier) @n)")
    assert [hit.text for hit in funcs] == ["Alpha"]
    methods = query(session, name, "(method_declaration name: (field_identifier) @n)")
    assert [hit.text for hit in methods] == ["delta"]


def test_rust_function_and_struct_names(session):
    name, text = sample_text("rust")
    session.write(name, text)
    funcs = query(session, name, "(function_item name: (identifier) @n)")
    assert [hit.text for hit in funcs] == ["alpha", "gamma"]
    structs = query(session, name, "(struct_item name: (type_identifier) @n)")
    assert [hit.text for hit in structs] == ["Beta"]


def test_c_function_name_feeds_splice(session):
    name, text = sample_text("c")
    session.write(name, text)
    names = query(
        session,
        name,
        "(function_definition declarator: "
        "(function_declarator declarator: (identifier) @n))",
    )
    assert [hit.text for hit in names] == ["alpha"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "omega")
        for hit in names
    ]
    assert session.splice(name, spans) == 1
    assert "int omega(int x)" in session.read(name)


def test_cpp_function_and_class_names(session):
    name, text = sample_text("cpp")
    session.write(name, text)
    funcs = query(
        session,
        name,
        "(function_definition declarator: "
        "(function_declarator declarator: (identifier) @n))",
    )
    assert [hit.text for hit in funcs] == ["alpha"]
    classes = query(session, name, "(class_specifier name: (type_identifier) @n)")
    assert [hit.text for hit in classes] == ["Beta"]


def test_java_method_and_class_names(session):
    name, text = sample_text("java")
    session.write(name, text)
    methods = query(session, name, "(method_declaration name: (identifier) @n)")
    assert [hit.text for hit in methods] == ["alpha", "gamma"]
    classes = query(session, name, "(class_declaration name: (identifier) @n)")
    assert [hit.text for hit in classes] == ["Beta"]


def test_ruby_method_and_class_names(session):
    name, text = sample_text("ruby")
    session.write(name, text)
    methods = query(session, name, "(method name: (identifier) @n)")
    assert [hit.text for hit in methods] == ["alpha", "gamma"]
    classes = query(session, name, "(class name: (constant) @n)")
    assert [hit.text for hit in classes] == ["Beta"]


def test_lua_function_name(session):
    name, text = sample_text("lua")
    session.write(name, text)
    names = query(session, name, "(function_declaration name: (identifier) @n)")
    assert [hit.text for hit in names] == ["alpha"]


def test_json_pair_keys(session):
    name, text = sample_text("json")
    session.write(name, text)
    keys = query(session, name, "(pair key: (string) @k)")
    assert [hit.text for hit in keys] == ['"alpha"', '"beta"']


def test_yaml_mapping_keys(session):
    name, text = sample_text("yaml")
    session.write(name, text)
    keys = query(session, name, "(block_mapping_pair key: (flow_node) @k)")
    assert [hit.text for hit in keys] == ["alpha", "beta", "gamma"]


def test_toml_key_feeds_splice(session):
    name, text = sample_text("toml")
    session.write(name, text)
    keys = query(session, name, "(pair (bare_key) @k)")
    assert [hit.text for hit in keys] == ["alpha", "gamma"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "omega")
        for hit in keys[:1]
    ]
    assert session.splice(name, spans) == 1
    assert "omega = 1" in session.read(name)


def test_bash_function_name(session):
    name, text = sample_text("bash")
    session.write(name, text)
    names = query(session, name, "(function_definition name: (word) @n)")
    assert [hit.text for hit in names] == ["alpha"]


def test_tsx_function_and_method_names(session):
    name, text = sample_text("tsx")
    session.write(name, text)
    funcs = query(session, name, "(function_declaration name: (identifier) @n)")
    assert [hit.text for hit in funcs] == ["App"]
    methods = query(session, name, "(method_definition name: (property_identifier) @n)")
    assert [hit.text for hit in methods] == ["render"]


def test_zig_function_name(session):
    name, text = sample_text("zig")
    session.write(name, text)
    names = query(session, name, "(function_declaration name: (identifier) @n)")
    assert [hit.text for hit in names] == ["alpha"]


def test_css_selector_names_feed_splice(session):
    name, text = sample_text("css")
    session.write(name, text)
    classes = query(session, name, "(rule_set (selectors (class_selector) @s))")
    assert [hit.text for hit in classes] == [".alpha"]
    ids = query(session, name, "(rule_set (selectors (id_selector) @s))")
    assert [hit.text for hit in ids] == ["#beta"]
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, ".omega")
        for hit in classes
    ]
    assert session.splice(name, spans) == 1
    assert ".omega {" in session.read(name)


def test_fish_function_name(session):
    name, text = sample_text("fish")
    session.write(name, text)
    names = query(session, name, "(function_definition name: (word) @n)")
    assert [hit.text for hit in names] == ["alpha"]


def test_zsh_function_name(session):
    name, text = sample_text("zsh")
    session.write(name, text)
    names = query(session, name, "(function_definition name: (word) @n)")
    assert [hit.text for hit in names] == ["alpha"]


def test_markdown_headings_and_fence_language(session):
    name, text = sample_text("markdown")
    session.write(name, text)
    heads = query(session, name, "(atx_heading) @h")
    assert [hit.text for hit in heads] == ["# Alpha\n", "## Beta\n"]
    langs = query(session, name, "(fenced_code_block (info_string (language) @l))")
    assert [hit.text for hit in langs] == ["python"]


def test_predicate_filters_hits(session):
    name, text = sample_text("nix")
    session.write(name, text)
    hits = query(session, name, '(binding (attrpath) @a (#eq? @a "alpha"))')
    assert [(hit.capture, hit.text) for hit in hits] == [("a", "alpha")]


def test_svelte_tag_names_feed_splice(session):
    name, text = sample_text("svelte")
    session.write(name, text)
    heads = query(session, name, '((tag_name) @t (#eq? @t "h1"))')
    assert len(heads) == 2  # start and end tags
    spans = [
        (hit.start_line, hit.start_column, hit.end_line, hit.end_column, "h2")
        for hit in heads
    ]
    assert session.splice(name, spans) == 2
    assert "<h2>Hello {name}!</h2>" in session.read(name)


def test_hits_arrive_ordered_by_position(session):
    name, text = sample_text("nix")
    session.write(name, text)
    hits = query(session, name, "(binding) @b")
    positions = [(hit.start_line, hit.start_column) for hit in hits]
    assert positions == sorted(positions)
    assert [hit.kind for hit in hits] == ["binding", "binding"]


def test_kinds_names_the_nix_vocabulary(session):
    name, text = sample_text("nix")
    session.write(name, text)
    found = kinds(session, name)
    assert "binding" in found["kinds"]
    assert "attrpath" in found["kinds"]
    assert "attrpath" in found["fields"]
    assert found["kinds"] == sorted(found["kinds"])


def test_query_reads_staged_content(session):
    session.write("a.py", "old_name = 1\n")
    session.write("a.py", "renamed_thing = 1\n")
    hits = query(session, "a.py", "(identifier) @i")
    assert [hit.text for hit in hits] == ["renamed_thing"]


def test_query_unknown_suffix_raises(session):
    session.write("blob.xyz", "whatever\n")
    with pytest.raises(ValueError, match="no tree-sitter grammar"):
        query(session, "blob.xyz", "(identifier) @i")


def test_kinds_unknown_suffix_raises(session):
    session.write("blob.xyz", "whatever\n")
    with pytest.raises(ValueError, match="no tree-sitter grammar"):
        kinds(session, "blob.xyz")


def test_bad_pattern_raises_naming_position(session):
    from tree_sitter import QueryError

    name, text = sample_text("nix")
    session.write(name, text)
    with pytest.raises(QueryError):
        query(session, name, "(bindin) @b")


def test_pins_cover_every_sampled_language():
    from pyedit.patterns import extract
    from pyedit.syntax.rules import RULES

    pinned = set(extract(Path(__file__)))
    sampled = {
        path.parent.name for path in CORPUS.rglob("sample.*") if path.suffix in RULES
    }
    assert not sampled - pinned, sampled - pinned


def test_generated_patterns_are_current():
    from pyedit import patterns

    drift = patterns.check(Path(__file__))
    assert drift == "", f"run `python3 -m pyedit.patterns --write`:\n{drift}"


def test_corpus_covers_every_shipped_language():
    from pyedit.syntax.rules import RULES

    have = {
        id(RULES[path.suffix])
        for path in CORPUS.rglob("sample.*")
        if path.suffix in RULES
    }
    assert {id(rules) for rules in RULES.values()} == have
