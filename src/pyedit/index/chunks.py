"""The units the index embeds: named definitions from `outline`, long
enough to say something."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pyedit import syntax

if TYPE_CHECKING:
    from pyedit.session import EditSession

MIN_LINES = 4
# Qwen3-Embedding reads 32k tokens; a class past this is still known by its head
MAX_CHARS = 16_000
# outline titles their keys and headings too, which are not code
DATA_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".md"})
# outline titles these by name, but they are call sites, not definitions:
# in nixidae every long `stores=[...]` argument paired with its twin
NOT_DEFINITIONS = frozenset({"keyword_argument"})


@dataclass(frozen=True)
class Chunk:
    kind: str
    name: str | None
    start_line: int
    end_line: int
    text: str


def digest(text: str) -> str:
    """A chunk's identity in the index: equal text, equal vector."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def indexable(path: str | Path) -> bool:
    suffix = Path(path).suffix
    return suffix not in DATA_SUFFIXES and syntax.known_language(suffix)


def chunks(session: EditSession, path: str | Path) -> list[Chunk]:
    """Definitions of at least MIN_LINES lines, nested ones included,
    in file order. A file without one yields nothing."""
    return [
        Chunk(
            kind=node.kind,
            name=node.name,
            start_line=node.start_line,
            end_line=node.end_line,
            text=node.text[:MAX_CHARS],
        )
        for node in syntax.outline(session, path)
        if node.end_line - node.start_line + 1 >= MIN_LINES
        and node.kind not in NOT_DEFINITIONS
    ]
