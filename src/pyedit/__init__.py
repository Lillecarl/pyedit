"""pyedit: scripted multi-file edits with dry-run diffs for AI agents."""

from collections.abc import Sequence
from pathlib import Path

from pyedit._version import __version__
from pyedit.lsp_client import LspSession
from pyedit.merge import Collision, VFS

__all__ = [
    "LspSession",
    "apply_diff",
    "apply_v4a",
    "Collision",
    "VFS",
    "__version__",
    "lsp",
]


def lsp(command: Sequence[str], python_path: str | Path | None = None) -> LspSession:
    """Bind a language server to the active edit session.

    The command runs from PATH or absolute, e.g. ["rust-analyzer"].
    Pass python_path when the server must know the interpreter to
    resolve imports (pyright drops qualified call sites otherwise).
    Library callers holding their own EditSession construct
    LspSession(session, command) directly.
    """
    from pyedit.active import current

    session = current() or globals().get("session")
    if session is None:
        raise AttributeError(
            "no edit session is running: construct LspSession(session, command) directly"
        )
    return LspSession(session, list(command), python_path=python_path)


def __getattr__(name: str):
    # PEP 562: forward API names to the active session -- the root, or
    # the VFS scope while its with-body runs -- so `import pyedit` and
    # the injected global behave identically in edit scripts
    if name.startswith("__"):
        raise AttributeError(name)
    from pyedit.active import current

    session = current() or globals().get("session")
    if session is not None and hasattr(session, name):
        return getattr(session, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r} "
        "(no edit session is running)"
    )
