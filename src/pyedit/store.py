"""Dry-run and undo artifacts.

A patch is saved under a short random id in a shared temp directory,
so an agent can apply it later without resending the script:
``pyedit apply <id>``. Beside it, ``<id>.json`` records the kind and
the root its paths are relative to, so a replay targets the same
files from any working directory. Files are 0o600 (diffs can carry
sensitive content) and are left for the OS temp cleaner to reap.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Literal

Kind = Literal["dry-run", "undo"]


def store_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "pyedit-dryruns"
    directory.mkdir(exist_ok=True)
    return directory


def save(diff_text: str, kind: Kind = "dry-run", root: Path | None = None) -> str:
    """Store a diff whose paths are relative to `root`; return its
    short id."""
    while True:
        token = secrets.token_hex(4)
        target = store_dir() / f"{token}.diff"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(diff_text)
        meta = {"kind": kind, "root": None if root is None else str(root)}
        fd = os.open(
            store_dir() / f"{token}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(meta, handle)
        return token


def root_of(token: str) -> Path | None:
    """The root a stored patch's paths are relative to; None for an id
    stored without one."""
    try:
        meta = json.loads((store_dir() / f"{token}.json").read_text())
    except (OSError, ValueError):
        return None
    root = meta.get("root") if isinstance(meta, dict) else None
    return Path(root) if isinstance(root, str) else None


def resolve(token: str) -> Path | None:
    """The stored diff for an id, or None when there is none."""
    if "/" in token or token.endswith(".diff"):
        return None
    candidate = store_dir() / f"{token}.diff"
    return candidate if candidate.is_file() else None
