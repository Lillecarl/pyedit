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
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

from pyedit import runner

_LOCK = threading.Lock()


def run_edit(
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
) -> dict:
    """The single tool's core, without any MCP surface, so tests can
    call it directly. Always returns a dict; failures are `ok: False`
    with an `error`, never raised."""
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

    from pyedit import watchdog

    with _LOCK:
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
            )

            def stage(session):
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
            else:
                session = runner.new_session(opts)
                try:
                    stage(session)
                except Exception:
                    return {"ok": False, "error": traceback.format_exc(limit=5)}
                try:
                    result = runner.finish(session, opts)
                except Exception as err:
                    return {"ok": False, "error": f"pyedit: {err}"}
        finally:
            if fuse is not None:
                fuse.set()

    if result.refused:
        return {
            "ok": False,
            "error": "syntax check failed; nothing was written "
            "(fix the files, or run with force)",
            "diff": result.diff,
            "problems": result.problems,
        }
    return {
        "ok": True,
        "diff": result.diff,
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


def create_server():
    """Build the MCP server. Imported lazily: only `pyedit mcp`
    needs the `mcp` package installed."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server

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
    ) -> dict:
        """Run a pyedit edit script. See `pyedit skill` for the session API."""
        return run_edit(
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
        )

    return server


def serve_stdio() -> int:
    create_server().run(transport="stdio")
    return 0
