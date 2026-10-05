"""The configured LSP pass: code actions and formatting from language
servers, over staged files only.

Runs after the script and before the ``[format]`` pass. Each
``[lsp.*]`` table starts its server once, applies its actions in
listed order (then formatting when enabled) to every staged text
file with a matching suffix, and re-stages results through the
session -- the diff, the stored patch and undo all carry them. Files
matching `exclude` are skipped by both this pass and ``[format]``.

Never starts a server nothing matches. A server that fails to start,
or an action that fails, fails the run with the table named;
nothing is written.
"""

from __future__ import annotations

from pathlib import Path

from pyedit.config import Config
from pyedit.session import EditSession, Symlink, display_path


class LspPassError(Exception):
    """A configured LSP pass failed: the server would not start, or
    an action failed. The run fails closed; nothing is written."""


def apply(
    session: EditSession,
    staged: dict[Path, str | bytes | None],
    config: Config,
) -> list[str]:
    """Run every [lsp.*] table over its staged files; return the
    files changed as `display (server)` strings."""
    from pyedit.lsp_client import LspSession

    touched: list[str] = []
    if not config.lsp or not staged:
        return touched
    for name, table in config.lsp.items():
        paths = [
            path
            for path in sorted(staged)
            if _eligible(session, staged[path], path, table.suffixes, config)
        ]
        if not paths:
            continue
        try:
            with LspSession(session, table.command) as lsp:
                for path in paths:
                    before = session.read(path)
                    for kind in table.actions:
                        lsp.code_action(path, kind, only_titles=table.only_titles)
                    if table.format:
                        lsp.format_file(path)
                    if session.read(path) != before:
                        touched.append(f"{display_path(path)} ({name})")
        except Exception as err:
            raise LspPassError(f"lsp.{name}: {err}") from err
    return touched


def _eligible(
    session, content, path: Path, suffixes: list[str], config: Config
) -> bool:
    if not isinstance(content, str) or isinstance(content, Symlink):
        return False
    if path.suffix.lstrip(".") not in suffixes:
        return False
    return not config.excluded(session.root, session.relpath(path))
