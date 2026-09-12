import pytest

from pyedit.patch import PatchOperation, apply_patch, parse_patch
from pyedit.session import EditSession

ENVELOPE = """\
*** Begin Patch
*** Update File: src/a.py
@@
-alpha = 1
+alpha = 42
*** Add File: src/new.py
+fresh = True
*** Delete File: src/b.py
*** End Patch
"""


def test_parse_patch_reads_all_operations():
    operations = parse_patch(ENVELOPE)
    assert [op.type for op in operations] == [
        "update_file",
        "create_file",
        "delete_file",
    ]
    assert operations[0].path == "src/a.py"
    assert operations[1].diff == "+fresh = True\n"


def test_parse_patch_requires_envelope():
    with pytest.raises(ValueError):
        parse_patch("no envelope here")
    with pytest.raises(ValueError):
        parse_patch("*** Begin Patch\n*** End Patch")


def test_parse_patch_move_to():
    operations = parse_patch(
        "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch\n"
    )
    assert operations[0].move_to == "b.py"
    assert operations[0].type == "update_file"


def test_apply_patch_stages_all_operations(project):
    session = EditSession()
    applied = apply_patch(session, ENVELOPE)
    assert len(applied) == 3
    staged = session.staged()
    assert staged[project / "src" / "a.py"] == "alpha = 42\nbeta = 2\n"
    assert staged[project / "src" / "new.py"] == "fresh = True"
    assert staged[project / "src" / "b.py"] is None


def test_deleting_a_pre_existing_file_renders_a_removal(project):
    session = EditSession()
    apply_patch(session, "*** Begin Patch\n*** Delete File: src/a.py\n*** End Patch\n")
    assert "+++ /dev/null" in session.diff()
    assert "-alpha = 1" in session.diff()


def test_create_then_delete_nets_to_nothing(project):
    # a file created and deleted in the same session has no net change:
    # neither side exists, so the diff is empty by design
    session = EditSession()
    apply_patch(session, "*** Begin Patch\n*** Add File: src/tmp.py\n+x = 1\n*** End Patch\n")
    apply_patch(session, "*** Begin Patch\n*** Delete File: src/tmp.py\n*** End Patch\n")
    assert session.diff() == ""


def test_apply_patch_update_with_move_to(project):
    session = EditSession()
    apply_patch(
        session,
        "*** Begin Patch\n"
        "*** Update File: src/a.py\n"
        "*** Move to: src/renamed.py\n"
        "@@\n"
        "-alpha = 1\n"
        "+alpha = 2\n"
        "*** End Patch\n",
    )
    staged = session.staged()
    assert staged[project / "src" / "a.py"] is None
    assert staged[project / "src" / "renamed.py"] == "alpha = 2\nbeta = 2\n"


def test_apply_patch_anchor_names_context_above(project):
    (project / "src" / "a.py").write_text("def f():\n    alpha = 1\n")
    session = EditSession()
    apply_patch(
        session,
        "*** Begin Patch\n"
        "*** Update File: src/a.py\n"
        "@@ def f():\n"
        "-    alpha = 1\n"
        "+    alpha = 2\n"
        "*** End Patch\n",
    )
    assert session.staged()[project / "src" / "a.py"] == "def f():\n    alpha = 2\n"


def test_apply_patch_update_missing_file_raises(project):
    session = EditSession()
    with pytest.raises(FileNotFoundError):
        apply_patch(
            session,
            "*** Begin Patch\n*** Update File: src/nope.py\n@@\n-x\n+y\n*** End Patch\n",
        )


def test_apply_patch_unchanged_hunk_prunes_clean(project):
    session = EditSession()
    apply_patch(
        session,
        "*** Begin Patch\n*** Update File: src/a.py\n@@\n-alpha = 1\n+alpha = 1\n*** End Patch\n",
    )
    session.prune_unchanged()
    assert session.staged() == {}


def test_apply_patch_binary_update_raises(project, tmp_path):
    (project / "src" / "data.bin").write_bytes(b"\xff\xfe\x00\x01")
    session = EditSession()
    with pytest.raises(ValueError, match="binary"):
        apply_patch(
            session,
            "*** Begin Patch\n*** Update File: src/data.bin\n@@\n-x\n+y\n*** End Patch\n",
        )


def test_patch_operation_defaults():
    operation = PatchOperation(type="delete_file", path="x")
    assert operation.diff is None
    assert operation.move_to is None
