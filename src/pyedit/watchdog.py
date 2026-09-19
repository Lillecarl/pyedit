"""Run watchdog: on timeout, dump every thread's stack into pyedit's
XDG state directory and kill the process.

One watchdog is armed per process: arming a new one disarms the
previous, so repeated runs never accumulate fuses -- a stale one
would otherwise kill a long-lived process long after its run ended.

Dumps land in `$XDG_STATE_HOME/pyedit/dumps` (default
`~/.local/state/pyedit/dumps`) -- the state class per the XDG spec:
logs and history, not cache and not config. The file name carries a
timestamp and the pid. Only the newest few are kept.

The raw os/open callables are captured at import time: the watchdog
thread runs inside the VFS-patched process during a script run, where
patched open/mkdir would stage the dump into the dying overlay instead
of writing it anywhere.
"""

from __future__ import annotations

import builtins
import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

_KEEP = 10
KEEP = 10
_REAL_OPEN = builtins.open
_REAL_MKDIR = os.mkdir
_REAL_UNLINK = os.unlink


def _real_makedirs(target: Path) -> None:
    """makedirs through the raw mkdir only: the real os.makedirs calls
    back into os.mkdir, which the VFS patch replaces."""
    stack = []
    current = target
    while current != current.parent:
        stack.append(current)
        current = current.parent
    for part in reversed(stack):
        try:
            _REAL_MKDIR(part)
        except FileExistsError:
            pass


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "pyedit"


def dump_dir() -> Path:
    return state_dir() / "dumps"


_armed: threading.Event | None = None
_running: threading.Thread | None = None


def start(seconds: float) -> threading.Event | None:
    """Arm the watchdog for this run; None when disabled. Arming a new
    watchdog disarms the previous one. Callers may cancel early with
    the returned event."""
    global _armed, _running
    if seconds <= 0:
        return None
    if _armed is not None:
        _armed.set()
    if _running is not None:
        # setting the event only wakes the thread; join so the fuse is
        # gone, not merely disarmed, by the time start returns
        _running.join(timeout=5)
        _running = None
    armed = threading.Event()
    _armed = armed

    def run():
        if armed.wait(seconds):
            return
        _dump_and_exit(seconds)

    thread = threading.Thread(target=run, name="pyedit-watchdog", daemon=True)
    thread.start()
    _running = thread
    return armed


def _dump_and_exit(seconds: float) -> None:
    path = None
    try:
        target = dump_dir()
        _real_makedirs(target)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = target / f"timeout-{stamp}-{os.getpid()}.log"
        with _REAL_OPEN(path, "w") as handle:
            faulthandler.dump_traceback(file=handle, all_threads=True)
        _prune()
    except OSError:
        pass  # a dead process needs no dump directory
    try:
        where = f"; thread stacks in {path}" if path else ""
        sys.stderr.write(f"pyedit: timed out after {seconds}s{where}\n")
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(124)


def _prune(keep: int = _KEEP) -> None:
    dumps = sorted(dump_dir().glob("timeout-*.log"))
    for old in dumps[:-keep]:
        _REAL_UNLINK(old)

