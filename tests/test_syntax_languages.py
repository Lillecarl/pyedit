"""The per-language syntax matrix, library level: a correct sample
must parse clean and outline exactly its definitions; a broken sample
must surface problems. Corpus lives in syntax_corpus.py, the CLI-level
gate for the same rows lives in test_cli.py."""

import pytest

from pyedit.session import EditSession
from pyedit.syntax import node_at, outline, problems
from pyedit.syntax.rules import RULES
from syntax_corpus import LANGUAGES


def test_table_covers_every_shipped_suffix():
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
        info = node_at(session, path, first.start_line, first.start_column)
        titled = info.name or (info.enclosing.name if info.enclosing else None)
        assert titled == first.name, f"{name}: node_at missed {first.name}"

    session.write(path, broken)
    found = problems(session, path)
    assert found, f"{name}: broken sample not flagged"
    assert all(p.line >= 1 and p.column >= 0 for p in found)
    assert all(p.message for p in found)
