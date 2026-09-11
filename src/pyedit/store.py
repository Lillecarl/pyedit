"""Dry-run artifacts.

A dry-run's diff is saved under a short random id in a shared temp
directory, so an agent can apply it later without resending the script:
``pyedit -d <id> -a``. Files are 0o600 (diffs can carry sensitive
content) and are left for the OS temp cleaner to reap.
"""

import os
import secrets
import tempfile
from pathlib import Path


def store_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "pyedit-dryruns"
    directory.mkdir(exist_ok=True)
    return directory


def save(diff_text: str) -> str:
    """Store a diff; return its short id."""
    while True:
        token = secrets.token_hex(4)
        target = store_dir() / f"{token}.diff"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(diff_text)
        return token


def resolve(token: str) -> Path | None:
    """The stored diff for an id, or None when there is none."""
    if "/" in token or token.endswith(".diff"):
        return None
    candidate = store_dir() / f"{token}.diff"
    return candidate if candidate.is_file() else None
