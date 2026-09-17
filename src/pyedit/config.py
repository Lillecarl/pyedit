"""pyedit.toml configuration: where files live and what they may say.

Layers, lowest precedence first; a later file overrides earlier ones
per key:

1. the config-home file (platformdirs: ``~/.config/pyedit/pyedit.toml``
   on Linux, ``~/Library/Application Support/pyedit/pyedit.toml`` on
   macOS)
2. whatever a pyedit.toml names in ``parent = "../pyedit.toml"``,
   resolved relative to that file -- for umbrella collections where
   several repos share one config; pointers may chain
3. ``pyedit.toml`` at the session root (the invocation directory)

A missing file is simply absent from the merge. A ``parent`` that
points at a missing file, a pointer cycle, invalid TOML, an unknown
key or a malformed ``[format]`` value is a loud ConfigError.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

__all__ = ["Config", "ConfigError", "config_file", "load"]


class ConfigError(Exception):
    """A pyedit.toml is malformed, or points at a missing file."""


@dataclass(frozen=True)
class Config:
    formatters: dict[str, list[str]] = field(default_factory=dict)


def config_file() -> Path:
    """The config-home layer: pyedit.toml in the user config dir."""
    return Path(user_config_dir("pyedit", appauthor=False)) / "pyedit.toml"


def load(root: Path) -> Config:
    """Merge every pyedit.toml layer for a session rooted at `root`."""
    merged: dict[str, list[str]] = {}
    for path, data in [*_chain(config_file()), *_chain(root / "pyedit.toml")]:
        for suffix, argv in data.get("format", {}).items():
            merged[suffix] = argv
    return Config(formatters=merged)


def _chain(start: Path) -> list[tuple[Path, dict]]:
    """`start` plus whatever its ``parent`` pointers lead to, parsed,
    outermost first. A missing `start` is an empty chain."""
    pairs: list[tuple[Path, dict]] = []
    seen: set[Path] = set()
    current = start
    while current.is_file():
        resolved = current.resolve()
        if resolved in seen:
            raise ConfigError(f"{current}: parent pointers form a cycle")
        seen.add(resolved)
        data = _read(current)
        pairs.append((current, data))
        pointer = data.pop("parent", None)
        if pointer is None:
            break
        current = (current.parent / pointer).resolve()
        if not current.is_file():
            raise ConfigError(
                f"{pairs[-1][0]}: parent {pointer!r} points at "
                f"{current}, which is not a file"
            )
    pairs.reverse()
    return pairs


def _read(path: Path) -> dict:
    """Parse and validate one pyedit.toml."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read pyedit.toml: {exc}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML in pyedit.toml: {exc}") from None
    unknown = set(data) - {"parent", "format"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(sorted(unknown))}; "
            "expected 'parent' and [format]"
        )
    parent = data.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise ConfigError(f"{path}: parent must be a non-empty path string")
    formatters = data.get("format", {})
    if not isinstance(formatters, dict):
        raise ConfigError(
            f"{path}: [format] must be a table of suffix = [command ...]"
        )
    normalized = {}
    for suffix, argv in formatters.items():
        suffix = suffix.lstrip(".")
        if not suffix:
            raise ConfigError(f"{path}: [format] key needs a file suffix")
        normalized[suffix] = _argv(path, suffix, argv)
    data["format"] = normalized
    return data


def _argv(path: Path, suffix: str, argv) -> list[str]:
    where = f"{path}: [format].{suffix}"
    if not isinstance(argv, list) or not argv:
        raise ConfigError(f"{where} must be a non-empty list of strings")
    if not all(isinstance(word, str) and word for word in argv):
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return argv
