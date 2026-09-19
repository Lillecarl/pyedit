"""The rendered skill document, checked as the contract it is.

Escapes in skill.py are load-bearing: SKILL_HEAD and SKILL_TAIL are
ordinary strings, so a single backslash in an example silently becomes
an escape sequence. These catch that.
"""

import pytest

from pyedit.skill import render_skill


def test_no_stray_control_characters():
    """`r"args.\\1"` written as `r"args.\1"` prints 0x01, not a
    backreference. It did, before 2026-09-14."""
    text = render_skill()
    bad = sorted(
        {ch for ch in text if ord(ch) < 32 and ch not in "\n\t"}
    )
    assert not bad, f"control characters in the skill: {[hex(ord(c)) for c in bad]}"


# a distinctive line from each python example; the API listing and the
# shell blocks are prose and heredocs, not code to compile
EXAMPLES = (
    'pyedit.edit_re("src/app.py"',
    "import ast",
    "with pyedit.VFS():",
    "with pyedit.lsp(",
    'Path("src/new.py")',
)


def _block_containing(text: str, marker: str) -> str:
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("    ") and line.strip():
            current.append(line[4:])
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    found = [b for b in blocks if marker in b]
    assert len(found) == 1, f"{marker!r} found in {len(found)} blocks"
    return found[0]


@pytest.mark.parametrize("marker", EXAMPLES)
def test_every_python_example_compiles(marker):
    """An agent copies these verbatim, so they must be valid python."""
    compile(_block_containing(render_skill(), marker), "<skill>", "exec")


def test_the_named_apis_are_all_documented():
    text = render_skill()
    for name in (
        "apply_v4a",
        "apply_diff_git",
        "apply_diff_unidiff",
        "diff_git",
        "diff_difflib",
        "pyedit apply ID",
        "--no-rename-detection",
        "read_fd",
        "pyedit.toml",
        "[format]",
    ):
        assert name in text, name


def test_the_frontmatter_makes_it_a_skill_file():
    """Installed as SKILL.md; a harness reads the header before
    it loads the body."""
    text = render_skill()
    assert text.startswith("---\n")
    head, closed, body = text[4:].partition("---\n")
    assert closed, "the frontmatter block is not closed"
    fields = dict(line.split(": ", 1) for line in head.splitlines() if line)
    assert fields["name"] == "pyedit", fields
    assert len(fields["description"]) < 1024
    assert body.lstrip("\n").startswith("# pyedit")


def test_it_stays_compact():
    """A budget, not a target: it is loaded into a context window."""
    text = render_skill()
    assert len(text) < 18000, f"skill grew to {len(text)} bytes"
