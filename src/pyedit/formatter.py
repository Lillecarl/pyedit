"""The configured formatter pass: staged text in on stdin, the
formatter's stdout is re-staged as the file's final content.

Runs at the end of a script run, after the script has staged its
edits and before the syntax check and diff. Each staged text file
whose suffix has a command in pyedit.toml's ``[format]`` is piped
through the formatter; because the result lands through
EditSession.write, the printed diff, the stored patch and undo all
carry the formatted text and apply/undo need nothing new.

A formatter runs as a subprocess, which escapes the stdlib overlay by
design -- so the pass holds the text itself and pipes it instead of
pointing a tool at a path. Formatters that need a file on disk are
not supported yet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pyedit.session import EditSession, Symlink

__all__ = ["FormatterError", "format_staged"]


class FormatterError(Exception):
    """A configured formatter failed: command missing, non-zero exit,
    or no output. The run fails closed; nothing is written."""


def format_staged(
    session: EditSession,
    staged: dict[Path, str | bytes | None],
    formatters: dict[str, list[str]],
) -> list[Path]:
    """Format staged text in place; return the paths whose content
    changed. Bytes, symlinks and deletions are never formatted."""
    changed: list[Path] = []
    for path in sorted(staged):
        content = staged[path]
        if not isinstance(content, str) or isinstance(content, Symlink):
            continue
        argv = formatters.get(path.suffix.lstrip("."))
        if not argv:
            continue
        out = _run(session, argv, content, path)
        if out != content:
            session.write(path, out)
            changed.append(path)
    return changed


def _run(
    session: EditSession, argv: list[str], text: str, path: Path
) -> str:
    cmd = [word.replace("{path}", str(path)) for word in argv]
    try:
        done = subprocess.run(
            cmd,
            cwd=session.root,
            input=text,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        raise FormatterError(
            f"formatter command not found: {cmd[0]} "
            f"(configured for .{path.suffix.lstrip('.')})"
        ) from None
    except UnicodeDecodeError:
        raise FormatterError(
            f"formatter {' '.join(cmd)} on {path}: output was not utf-8"
        ) from None
    if done.returncode != 0:
        raise FormatterError(
            f"formatter {' '.join(cmd)} failed on {path}: "
            f"exit {done.returncode}\n{done.stderr.strip()}"
        )
    if not done.stdout and text:
        raise FormatterError(
            f"formatter {' '.join(cmd)} wrote nothing to stdout for "
            f"{path}; it likely has no stdin mode"
        )
    return done.stdout
