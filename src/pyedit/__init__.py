"""pyedit: scripted multi-file edits with dry-run diffs for AI agents."""

__version__ = "0.1.0"


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
