"""pyedit: scripted multi-file edits with dry-run diffs for AI agents."""

from collections.abc import Sequence

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


def lsp(command: Sequence[str]) -> LspSession:
    """Bind a language server to the live edit session.

    The command runs from PATH or absolute, e.g. ["rust-analyzer"].
    Library callers holding their own EditSession construct
    LspSession(session, command) directly.
    """
    session = globals().get("session")
    if session is None:
        raise AttributeError(
            "no edit session is running: construct LspSession(session, command) directly"
        )
    return LspSession(session, list(command))


def __getattr__(name: str):
    # PEP 562: forward API names to the live session, so `import pyedit`
    # and the injected global behave identically in edit scripts
    if name.startswith("__"):
        raise AttributeError(name)
    session = globals().get("session")
    if session is not None and hasattr(session, name):
        return getattr(session, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r} "
        "(no edit session is running)"
    )
