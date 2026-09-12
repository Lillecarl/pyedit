"""The active-session stack.

Scripts always talk to ``pyedit.*``, which routes to the top of this
stack: the root session normally, a scope while its ``with`` body
runs. ``VFS`` pushes and pops; nothing else touches it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyedit.session import EditSession

_stack: list["EditSession"] = []


def push(session: "EditSession") -> None:
    _stack.append(session)


def pop() -> "EditSession":
    return _stack.pop()


def current() -> "EditSession | None":
    return _stack[-1] if _stack else None
