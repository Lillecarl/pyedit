"""pyedit as an MCP server, over stdio.

One tool, `run`: it takes an edit script (inline or from a file) and
runs it through the same runner as the CLI -- dry-run diff by
default, writes on `apply`, stored ids replay via `apply_id`. Tool
and parameter descriptions stay minimal; `pyedit skill` is the
documentation agents read.

The `mcp` package is imported lazily so the CLI never needs it. Both
its v1 (`FastMCP`) and v2 (`MCPServer`) APIs are accepted: only the
common subset (`tool`, `run`) is used. stdio only -- stdout carries
the protocol, so nothing here prints.

Each tool call runs in a fresh process forked from a forkserver
(`run_in_child`). Python cannot stop a thread stuck in C code -- a
blocking call, libgit2, a backtracking regex that holds the GIL
froze every thread when measured -- so a timeout kills that process
and the server keeps serving. Not a pool: a reused worker would carry
the VFS patches and cwd of an earlier run.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyedit import runner

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.context import ForkServerContext

    from pyedit.fdin import Payloads
    from pyedit.session import EditSession

_LOCK = threading.Lock()
# seconds past the timeout the parent waits for the child's own
# watchdog to dump stacks and exit, before it kills the child
_GRACE = 5.0
_WRITING = "writing"
# imported once in the forkserver, so a fork starts warm
_PRELOAD = [
    "pyedit.mcp_server",
    "pyedit.runner",
    "pyedit.session",
    "pyedit.diff",
    "pyedit.memgit",
    "pyedit.gitpatch",
    "pyedit.syntax",
    "pygit2",
]
_CONTEXT: ForkServerContext | None = None


def run_edit(
    script: str | None = None,
    script_file: str | None = None,
    fds: Payloads | None = None,
    apply: bool = False,
    apply_id: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    context: int = runner.DEFAULT_CONTEXT,
    workdir: str | None = None,
    revision: str | None = None,
    force: bool = False,
    timeout: float = 300.0,
    output: str | None = None,
    before_write: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """The single tool's core, in this process and without any MCP
    surface, so tests can call it directly. Always returns a dict;
    failures are `ok: False` with an `error`, never raised."""
    sources = [
        name
        for name, value in (
            ("script", script),
            ("script_file", script_file),
            ("apply_id", apply_id),
        )
        if value is not None
    ]
    if len(sources) != 1:
        return {
            "ok": False,
            "error": "pass exactly one of script, script_file, apply_id; "
            f"got: {', '.join(sources) or 'none'}",
        }
    if context < 0:
        return {
            "ok": False,
            "error": f"context is {context}; it is lines, not less than 0",
        }
    if fds is not None:
        for key, value in fds.items():
            try:
                int(key)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"fds keys are FD numbers; got {key!r}"}
            if not isinstance(value, str):
                return {
                    "ok": False,
                    "error": f"fds[{key!r}] must be str, not {type(value).__name__}",
                }

    root: Path | None = None
    if workdir is not None:
        root = Path(workdir).expanduser()
        if not root.is_dir():
            return {"ok": False, "error": f"workdir is not a directory: {workdir}"}

    diff_file: Path | None = None
    if output is not None:
        diff_file = Path(output).expanduser()
        if not diff_file.is_absolute():
            diff_file = (root or Path.cwd()) / diff_file

    text: str
    filename: str
    mode = "script"
    if script_file is not None:
        candidate = Path(script_file).expanduser()
        if not candidate.is_absolute():
            candidate = (root or Path.cwd()) / candidate
        try:
            text = candidate.read_text()
        except OSError as err:
            return {
                "ok": False,
                "error": f"script_file {candidate}: {err.strerror or err}",
            }
        filename = candidate.as_posix()
    elif script is not None:
        text = script
        filename = "<mcp-script>"
    else:
        from pyedit import store as _store

        assert apply_id is not None
        stored = _store.resolve(apply_id)
        if stored is None:
            return {
                "ok": False,
                "error": f"no stored dry-run {apply_id!r}; ids come from earlier runs",
            }
        try:
            text = stored.read_text()
        except OSError as err:
            return {
                "ok": False,
                "error": f"stored dry-run {apply_id!r}: {err.strerror or err}",
            }
        mode = "stored"
        filename = stored.as_posix()
        root = _store.root_of(apply_id) or root

    from pyedit import watchdog

    with _LOCK, runner.working_directory(root):
        fuse = watchdog.start(timeout)
        try:
            opts = runner.Options(
                include=include,
                exclude=exclude,
                context=context,
                apply=True if mode == "stored" else apply,
                force=force,
                skip_format=(mode != "script"),
                root=root,
                revision=revision,
                before_write=before_write,
            )

            def stage(session: EditSession) -> None:
                if mode == "stored":
                    runner.replay_stored(session, text)
                else:
                    runner.execute_script(session, text, filename, fds)

            if revision is not None:
                from pyedit import jjrev

                try:
                    result = runner.run_revision(opts, revision, stage)
                except jjrev.JjRevError as err:
                    return {"ok": False, "error": f"pyedit: {err}"}
                except (Exception, SystemExit):
                    return {"ok": False, "error": traceback.format_exc(limit=5)}
            else:
                session = runner.new_session(opts)
                try:
                    stage(session)
                # SystemExit too: it would end the server, not the run
                except (Exception, SystemExit):
                    return {"ok": False, "error": traceback.format_exc(limit=5)}
                try:
                    result = runner.finish(session, opts)
                except Exception as err:
                    return {"ok": False, "error": f"pyedit: {err}"}
        finally:
            if fuse is not None:
                fuse.set()

    diff = result.diff
    shown: dict[str, Any] = {"diff": diff}
    if diff_file is not None:
        try:
            diff_file.parent.mkdir(parents=True, exist_ok=True)
            diff_file.write_text(diff)
        except OSError as err:
            return {
                "ok": False,
                "error": f"output {diff_file}: {err.strerror or err}; "
                + ("the run was applied" if result.applied else "nothing was written"),
                "undo_id": result.undo_id,
                "dry_run_id": result.dry_run_id,
            }
        shown = {"diff_file": str(diff_file)}
    if result.refused:
        return {
            "ok": False,
            "error": "syntax check failed; nothing was written "
            "(fix the files, or run with force)",
            **shown,
            "problems": result.problems,
        }
    return {
        "ok": True,
        **shown,
        "stat": [list(entry) for entry in result.stat],
        "files": result.files,
        "dry_run_id": result.dry_run_id,
        "undo_id": result.undo_id,
        "problems": result.problems,
        "formatted": result.formatted,
        "actions": result.actions,
        "applied": result.applied,
        "op": result.op,
        "cleanup_op": result.cleanup_op,
        "conflicts": result.conflicts,
        "meta": {k: [o, n] for k, (o, n) in result.meta.items()},
        "history": result.history,
    }


def _context() -> ForkServerContext:
    global _CONTEXT
    if _CONTEXT is None:
        import multiprocessing

        context = multiprocessing.get_context("forkserver")
        context.set_forkserver_preload(_PRELOAD)
        _CONTEXT = context
    return _CONTEXT


def run_in_child(timeout: float = 300.0, **kwargs: Any) -> dict[str, Any]:
    """`run_edit` in a fresh child process, which a timeout kills.

    The child gets this process's environment and cwd as they are now:
    a forkserver child would otherwise see them as they were when the
    forkserver started. What the script prints comes back as `stdout`;
    in the child, fd 1 would be the MCP protocol stream.
    """
    context = _context()
    fd, stdout_path = tempfile.mkstemp(prefix="pyedit-stdout-")
    os.close(fd)
    receive, send = context.Pipe(duplex=False)
    with _LOCK:
        proc = context.Process(
            target=_child_main,
            args=(send, kwargs, timeout, dict(os.environ), os.getcwd(), stdout_path),
            daemon=True,
        )
        proc.start()
        send.close()
        result: dict[str, Any] | None = None
        writing = False
        expired = False
        deadline = time.monotonic() + timeout + _GRACE if timeout > 0 else None
        while result is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if (remaining is not None and remaining <= 0) or not receive.poll(
                remaining
            ):
                expired = True
                break
            try:
                message = receive.recv()
            except EOFError:
                break
            if message == _WRITING:
                writing = True
            else:
                result = message
        # EOF arrives before the exit is reaped: an exiting child gets
        # the grace too, or it reads as one that had to be killed
        proc.join(0 if expired else _GRACE)
        killed = proc.is_alive()
        if killed:
            proc.kill()
            proc.join()
        receive.close()
    try:
        printed = Path(stdout_path).read_text(errors="replace")
    finally:
        os.unlink(stdout_path)
    if result is None:
        result = {
            "ok": False,
            "error": _death(proc.pid, proc.exitcode, killed, timeout, writing),
        }
    if printed:
        result["stdout"] = printed
    return result


def _death(
    pid: int | None, code: int | None, killed: bool, timeout: float, writing: bool
) -> str:
    from pyedit import watchdog

    if killed:
        why = (
            f"timed out after {timeout}s and did not stop for the watchdog "
            "(stuck outside Python code), so it was killed; no stack dump"
        )
    elif code == 124:
        dumps = sorted(watchdog.dump_dir().glob(f"timeout-*-{pid}.log"))
        where = f"; thread stacks in {dumps[-1]}" if dumps else ""
        why = f"timed out after {timeout}s{where}"
    else:
        why = f"its process died with exit code {code}"
    if writing:
        return (
            f"pyedit: the run {why}. It was writing files when it stopped, "
            "so they may be partial: check them with your VCS"
        )
    return f"pyedit: the run {why}; nothing was written"


def _child_main(
    send: Connection,
    kwargs: dict[str, Any],
    timeout: float,
    environ: dict[str, str],
    cwd: str,
    stdout_path: str,
) -> None:
    os.environ.clear()
    os.environ.update(environ)
    # gettempdir caches: the store must follow TMPDIR as shipped
    tempfile.tempdir = None
    os.chdir(cwd)
    fd = os.open(stdout_path, os.O_WRONLY)
    os.dup2(fd, 1)
    os.close(fd)

    def writing() -> None:
        send.send(_WRITING)

    try:
        result = run_edit(timeout=timeout, before_write=writing, **kwargs)
    except BaseException:
        result = {"ok": False, "error": traceback.format_exc(limit=5)}
    sys.stdout.flush()
    send.send(result)
    send.close()


def create_server():
    """Build the MCP server. Imported lazily: only `pyedit mcp`
    needs the `mcp` package installed."""
    # by name: v2 has only mcpserver, v1 only fastmcp, so a static
    # import fails the type check under one of them
    import importlib

    try:
        Server = importlib.import_module("mcp.server.mcpserver").MCPServer
    except ImportError:
        Server = importlib.import_module("mcp.server.fastmcp").FastMCP

    server = Server("pyedit")

    @server.tool()
    def run(
        script: str | None = None,
        script_file: str | None = None,
        fds: dict[str, str] | None = None,
        apply: bool = False,
        apply_id: str | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        context: int = runner.DEFAULT_CONTEXT,
        workdir: str | None = None,
        revision: str | None = None,
        force: bool = False,
        timeout: float = 300.0,
        output: str | None = None,
    ) -> dict[str, Any]:
        """Run a pyedit edit script. See `pyedit skill` for the session API."""
        return run_in_child(
            script=script,
            script_file=script_file,
            fds=fds,
            apply=apply,
            apply_id=apply_id,
            include=include,
            exclude=exclude,
            context=context,
            workdir=workdir,
            revision=revision,
            force=force,
            timeout=timeout,
            output=output,
        )

    return server


def serve_stdio() -> int:
    create_server().run(transport="stdio")
    return 0
