"""Read payloads attached as extra file descriptors.

A shell command can pipe content in on numbered FDs (`pyedit -s
script.py 3<<'EOF' 4<<'EOF'`); a quoted heredoc passes the bytes
through untouched. read_fd(n) hands such a payload to the script
verbatim -- the substitute for embedding escape-heavy text in Python
literals, and for /dev/fd paths, which reopen rather than share pipes.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping

_CHUNK = 1 << 16

_injected: dict[int, str] = {}


@contextlib.contextmanager
def injected(payloads: Mapping[str | int, str] | None) -> Iterator[None]:
    """Provide read_fd payloads without OS file descriptors.

    The MCP server has no shell to attach heredocs, so payloads
    arrive as a dict and read_fd serves them from memory. Keys are
    FD numbers, as strings (JSON objects) or ints.
    """
    if not payloads:
        yield
        return
    saved = dict(_injected)
    try:
        _injected.clear()
        for key, value in payloads.items():
            if not isinstance(value, str):
                raise TypeError(
                    f"fd payload {key!r} must be str, not {type(value).__name__}"
                )
            _injected[int(key)] = value
        yield
    finally:
        _injected.clear()
        _injected.update(saved)


def read_fd(n: int) -> str:
    """Read FD n to EOF and return it as str; the FD is consumed.

    Injected payloads (see injected) are served first and stay
    readable; real FDs are consumed as before."""
    if n in _injected:
        return _injected[n]
    chunks = []
    while True:
        try:
            chunk = os.read(n, _CHUNK)
        except OSError as err:
            raise OSError(
                f"pyedit.read_fd({n}): {err.strerror or err}: FD {n} is not "
                "open; attach it in the shell as N<<'EOF'"
            ) from err
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")
