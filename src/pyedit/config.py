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
key or a malformed value is a loud ConfigError.
"""

from __future__ import annotations

import fnmatch
import os
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
class LspTable:
    """One [lsp.*] server: the command, the suffixes it acts on, the
    code action kinds in order, title prefixes selecting among
    alternatives, and whether it formats last."""

    command: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    only_titles: list[str] = field(default_factory=list)
    format: bool = False


@dataclass(frozen=True)
class Config:
    formatters: dict[str, list[str]] = field(default_factory=dict)
    lsp: dict[str, LspTable] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)

    def excluded(self, root: Path, rel: str) -> bool:
        """Whether root-relative `rel` skips the format/lsp passes.

        Plain globs match the relative path; `/`- and
        `~`-prefixed globs match the absolute path instead.
        """
        absolute = (root / rel).as_posix()
        for pattern in self.exclude:
            if pattern.startswith(("~", "/")):
                if fnmatch.fnmatch(absolute, os.path.expanduser(pattern)):
                    return True
            elif fnmatch.fnmatch(rel, pattern):
                return True
        return False


def config_file() -> Path:
    """The config-home layer: pyedit.toml in the user config dir."""
    return Path(user_config_dir("pyedit", appauthor=False)) / "pyedit.toml"


def load(root: Path) -> Config:
    """Merge every pyedit.toml layer for a session rooted at `root`."""
    merged: dict[str, list[str]] = {}
    servers: dict[str, LspTable] = {}
    excluded: list[str] = []
    for path, data in [*_chain(config_file()), *_chain(root / "pyedit.toml")]:
        for suffix, argv in data.get("format", {}).items():
            merged[suffix] = argv
        for name, table in data.get("lsp", {}).items():
            servers[name] = table
        if "exclude" in data:
            excluded = data["exclude"]
    return Config(formatters=merged, lsp=servers, exclude=excluded)


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
    unknown = set(data) - {"parent", "format", "lsp", "exclude"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(sorted(unknown))}; "
            "expected 'parent', [format], [lsp] and 'exclude'"
        )
    parent = data.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise ConfigError(f"{path}: parent must be a non-empty path string")
    formatters = data.get("format", {})
    if not isinstance(formatters, dict):
        raise ConfigError(f"{path}: [format] must be a table of suffix = [command ...]")
    normalized = {}
    for suffix, argv in formatters.items():
        suffix = suffix.lstrip(".")
        if not suffix:
            raise ConfigError(f"{path}: [format] key needs a file suffix")
        normalized[suffix] = _argv(path, f"[format].{suffix}", argv)
    data["format"] = normalized
    lsp = data.get("lsp", {})
    if not isinstance(lsp, dict):
        raise ConfigError(f"{path}: [lsp] must be a table of name = {{...}}")
    data["lsp"] = {name: _lsp_table(path, name, table) for name, table in lsp.items()}
    data["exclude"] = _exclude(path, data.get("exclude", []))
    return data


def _lsp_table(path: Path, name: str, table) -> LspTable:
    where = f"{path}: [lsp.{name}]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - {"command", "suffixes", "actions", "only_titles", "format"}
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
            "expected 'command', 'suffixes', 'actions', 'only_titles' and 'format'"
        )
    suffixes = table.get("suffixes", [])
    if (
        not isinstance(suffixes, list)
        or not suffixes
        or not all(isinstance(s, str) and s.lstrip(".") for s in suffixes)
    ):
        raise ConfigError(f"{where} needs 'suffixes' as a non-empty list of suffixes")
    actions = table.get("actions", [])
    if not isinstance(actions, list) or not all(
        isinstance(a, str) and a for a in actions
    ):
        raise ConfigError(f"{where} needs 'actions' as a list of LSP kinds")
    only_titles = table.get("only_titles", [])
    if not isinstance(only_titles, list) or not all(
        isinstance(t, str) and t for t in only_titles
    ):
        raise ConfigError(f"{where} needs 'only_titles' as a list of title prefixes")
    formatting = table.get("format", False)
    if not isinstance(formatting, bool):
        raise ConfigError(f"{where} needs 'format' as true or false")
    if not actions and not formatting:
        raise ConfigError(f"{where} does nothing: give it actions or format = true")
    return LspTable(
        command=_argv(path, f"[lsp.{name}].command", table.get("command")),
        suffixes=[s.lstrip(".") for s in suffixes],
        actions=list(actions),
        only_titles=list(only_titles),
        format=formatting,
    )


def _exclude(path: Path, patterns) -> list[str]:
    if not isinstance(patterns, list) or not all(
        isinstance(p, str) and p for p in patterns
    ):
        raise ConfigError(f"{path}: 'exclude' must be a list of glob strings")
    return list(patterns)


def _argv(path: Path, where: str, argv) -> list[str]:
    where = f"{path}: {where}"
    if not isinstance(argv, list) or not argv:
        raise ConfigError(f"{where} must be a non-empty list of strings")
    if not all(isinstance(word, str) and word for word in argv):
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return argv
