"""pyedit.* routes to the active session -- and what shadows it.

`pyedit.__getattr__` (PEP 562) only runs for names Python does not
find on the module. A submodule of this package is found, so any
session method sharing a submodule name is unreachable from a script.
"""

import pyedit
from pyedit.session import EditSession

# every submodule name is a word a session method may not use
SHADOWED = {
    "active", "cli", "diff", "gitignore", "gitmerge", "gitpatch", "merge",
    "memgit", "patch", "render", "rope", "session", "skill", "store",
    "syntax", "udiff", "vendor", "vfs", "watchdog", "lsp_client",
}


def test_no_session_method_is_shadowed_by_a_submodule(project, monkeypatch):
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    public = {
        name
        for name in dir(session)
        if not name.startswith("_") and callable(getattr(session, name))
    }
    clashes = sorted(public & SHADOWED)
    assert not clashes, (
        f"unreachable through pyedit.*: {clashes}. "
        "pyedit.<name> resolves to the submodule, so a script calling it "
        "gets \"'module' object is not callable\"."
    )


def test_diff_git_is_callable_through_the_router(project, monkeypatch):
    session = EditSession()
    monkeypatch.setattr(pyedit, "session", session, raising=False)
    pyedit.write("src/a.py", "changed\n")
    assert "+changed" in pyedit.diff_git()


def test_diff_still_names_the_module(project):
    """Not a wart to fix: the module is the thing worth having here."""
    import types

    assert isinstance(pyedit.diff, types.ModuleType)
