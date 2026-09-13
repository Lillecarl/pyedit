import pytest

from pyedit.memgit import Entry, MemGitError, MemoryRepo, apply_to_files


BEFORE = {"pkg/mod.py": "one\ntwo\nthree\nfour\nfive\n"}

HUNK = """\
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -2,3 +2,3 @@
 two
-three
+THREE
 four
"""


def test_tree_writes_into_memory():
    # the pin for the GIL rule in memgit's docstring: Index.write_tree
    # runs through cffi, which drops the GIL, so a Python OdbBackend
    # here segfaults the whole test run instead of failing this test
    repo = MemoryRepo()
    tree = repo.tree({"pkg/mod.py": "one\n"})
    assert repo.read(tree["pkg/mod.py"].id) == b"one\n"


def test_applies_a_context_hunk():
    assert apply_to_files(BEFORE, HUNK) == {
        "pkg/mod.py": Entry("pkg/mod.py", b"one\ntwo\nTHREE\nfour\nfive\n", 0o100644)
    }


def test_creates_a_file():
    result = apply_to_files(
        BEFORE,
        "diff --git a/pkg/new.py b/pkg/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n+++ b/pkg/new.py\n"
        "@@ -0,0 +1,2 @@\n+alpha\n+beta\n",
    )
    assert result["pkg/new.py"].data == b"alpha\nbeta\n"
    assert result["pkg/mod.py"].data == BEFORE["pkg/mod.py"].encode()


def test_deletes_a_file():
    result = apply_to_files(
        BEFORE,
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "deleted file mode 100644\n"
        "--- a/pkg/mod.py\n+++ /dev/null\n"
        "@@ -1,5 +0,0 @@\n-one\n-two\n-three\n-four\n-five\n",
    )
    assert result == {}


def test_renames_a_file():
    result = apply_to_files(
        BEFORE,
        "diff --git a/pkg/mod.py b/pkg/renamed.py\n"
        "similarity index 100%\n"
        "rename from pkg/mod.py\nrename to pkg/renamed.py\n",
    )
    assert set(result) == {"pkg/renamed.py"}
    assert result["pkg/renamed.py"].data == BEFORE["pkg/mod.py"].encode()


def test_changes_the_file_mode():
    result = apply_to_files(
        BEFORE,
        "diff --git a/pkg/mod.py b/pkg/mod.py\nold mode 100644\nnew mode 100755\n",
    )
    assert result["pkg/mod.py"].mode == 0o100755


def test_keeps_a_missing_newline_at_eof():
    result = apply_to_files(
        {"pkg/mod.py": "one\ntwo\nthree"},
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n"
        "@@ -1,3 +1,3 @@\n one\n two\n-three\n"
        "\\ No newline at end of file\n+THREE\n"
        "\\ No newline at end of file\n",
    )
    assert result["pkg/mod.py"].data == b"one\ntwo\nTHREE"


def test_applies_a_binary_patch():
    # the payload comes from libgit2 too, so this round-trips pyedit's
    # only in-memory source of binary patches
    import pygit2

    repo = MemoryRepo()
    old = repo.blob(b"\x00\x01\x02\x03")
    new = repo.blob(b"\x00\x01\x02\x03\x04\x05")
    patch = old.diff(new, flags=pygit2.enums.DiffOption.SHOW_BINARY).text
    patch = patch.replace("a/file b/file", "a/x.bin b/x.bin")

    result = apply_to_files({"x.bin": b"\x00\x01\x02\x03"}, patch)
    assert result["x.bin"].data == b"\x00\x01\x02\x03\x04\x05"


def test_hunk_that_does_not_anchor_raises():
    # libgit2 checks one line position and does not search around it
    shifted = HUNK.replace("@@ -2,3 +2,3 @@", "@@ -1,3 +1,3 @@")
    with pytest.raises(MemGitError, match="did not apply"):
        apply_to_files(BEFORE, shifted)


def test_path_outside_the_tree_raises():
    with pytest.raises(MemGitError, match="does not exist"):
        apply_to_files(
            BEFORE,
            "diff --git a/pkg/absent.py b/pkg/absent.py\n"
            "--- a/pkg/absent.py\n+++ b/pkg/absent.py\n"
            "@@ -1 +1 @@\n-x\n+y\n",
        )


def test_unparseable_patch_raises():
    with pytest.raises(MemGitError, match="parse"):
        apply_to_files(BEFORE, "not a patch at all\n")
