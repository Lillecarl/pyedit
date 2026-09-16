"""Read payloads attached as extra file descriptors.

A shell command can pipe content in on numbered FDs (`pyedit -s
script.py 3<<'EOF' 4<<'EOF'`); a quoted heredoc passes the bytes
through untouched. read_fd(n) hands such a payload to the script
verbatim -- the substitute for embedding escape-heavy text in Python
literals, and for /dev/fd paths, which reopen rather than share pipes.
"""

from __future__ import annotations

import os

_CHUNK = 1 << 16


def read_fd(n: int) -> str:
    """Read FD n to EOF and return it as str; the FD is consumed."""
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
