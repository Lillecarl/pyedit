"""One edit run, without any CLI surface.

cli.main and the MCP server are both thin over this: execute a
script (or replay a stored patch) against a fresh session, then
finish() prunes, filters, formats, syntax-checks, diffs, stores
dry-run/undo ids and optionally applies. The Result is plain data;
presenting it -- stdout markers for the CLI, JSON for MCP -- is the
caller's job. Nothing here prints.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from pyedit.session import EditSession, Symlink, display_path

DEFAULT_CONTEXT = 3
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_FILES = 20000


@dataclass
class Options:
    include: list[str] | None = None
    exclude: list[str] | None = None
    context: int = DEFAULT_CONTEXT
    apply: bool = False
    force: bool = False
    skip_format: bool = False
    root: Path | None = None
    max_bytes: int | None = DEFAULT_MAX_BYTES
    max_files: int | None = DEFAULT_MAX_FILES
    respect_gitignore: bool = True
    find_renames: bool = True
    revision: str | None = None


@dataclass
class Result:
    diff: str = ""
    files: list[str] = field(default_factory=list)
    dry_run_id: str | None = None
    undo_id: str | None = None
    problems: list[str] = field(default_factory=list)
    formatted: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    applied: bool = False
    refused: bool = False
    op: str | None = None
    cleanup_op: str | None = None
    conflicts: list[str] = field(default_factory=list)
    # staged commit metadata as key -> (old, new): description
    # and/or author on -r runs, empty otherwise
    meta: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)


class MetaWithoutTarget(Exception):
    """describe()/author() staged commit metadata with no -r
    revision to apply it to: only -r amends a commit, so a plain run
    refuses instead of dropping the metadata silently."""


def allowed(rel: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    return True


def new_session(opts: Options) -> EditSession:
    return EditSession(
        max_bytes=opts.max_bytes,
        max_files=opts.max_files,
        respect_gitignore=opts.respect_gitignore,
        find_renames=opts.find_renames,
        root=opts.root,
    )


def execute_script(session: EditSession, text: str, filename: str, fds=None) -> None:
    """Run an edit script against the session's overlay.

    Exceptions propagate to the caller, which reports them; nothing
    is staged-halfway that finish() would mistake for a change, and
    the module global is restored for the next run in this process.
    """
    import pyedit
    from pyedit import vfs
    from pyedit.fdin import injected

    missing = object()
    previous = vars(pyedit).get("session", missing)
    pyedit.session = session
    previous_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        restore = vfs.install(session)
        try:
            with injected(fds):
                exec(
                    compile(text, filename, "exec"),
                    {"pyedit": pyedit, "__name__": "__main__"},
                )
        finally:
            restore()
    finally:
        sys.dont_write_bytecode = previous_dont_write
        if previous is missing:
            try:
                del pyedit.session
            except AttributeError:
                pass
        else:
            pyedit.session = previous


def replay_stored(session: EditSession, patch_text: str) -> None:
    """Replay one of pyedit's own canonical patches (a dry-run or
    undo id). libgit2 wrote it, so libgit2 applies it."""
    from pyedit import gitpatch as _gitpatch

    _gitpatch.apply_patch(session, patch_text)


def finish(
    session: EditSession,
    opts: Options,
    *,
    config_root: Path | None = None,
    store_ids: bool = True,
    allow_meta: bool = False,
) -> Result:
    """Prune, filter, format, syntax-check, diff, store ids, maybe
    apply. A refused apply (broken syntax without --force) returns a
    Result with refused set; nothing was written. Staged commit
    metadata (describe/author) needs a -r run to consume it: without
    allow_meta the run refuses naming that, instead of dropping it
    silently."""
    from pyedit import config, formatter, store
    from pyedit.diff import replayable_patch, unified_diffs
    from pyedit.syntax import render

    if session.meta and not allow_meta:
        raise MetaWithoutTarget(
            "pyedit.describe()/pyedit.author() stage commit metadata; "
            "only -r REV applies it to a commit"
        )
    session.prune_unchanged()

    def selected() -> dict[Path, str | bytes | None]:
        return {
            path: content
            for path, content in session.staged().items()
            if allowed(display_path(path), opts.include, opts.exclude)
        }

    staged = selected()

    # config-driven passes, script runs only -- stored replays apply
    # what was stored. Each pass re-stages through the session, so the
    # diff, the stored patch and undo all carry the final content.
    formatted: list[str] = []
    actions: list[str] = []
    if not opts.skip_format:
        from pyedit import lsppass as _lsppass

        conf = config.load(config_root or session.root)
        # language servers first: actions rewrite code, formatters
        # normalize text last
        actions = _lsppass.apply(session, staged, conf) if conf.lsp and staged else []
        if actions:
            session.prune_unchanged()
            staged = selected()
        touched = (
            formatter.format_staged(session, staged, conf.formatters, conf)
            if conf.formatters and staged
            else []
        )
        if touched:
            formatted = [f"{display_path(p)} ({p.suffix.lstrip('.')})" for p in touched]
            session.prune_unchanged()
            staged = selected()

    # staged text is parsed: problems surface here, in the same run
    # that shows the diff they would produce
    problems: list[str] = []
    for path in sorted(staged):
        value = staged[path]
        if not isinstance(value, str) or isinstance(value, Symlink):
            continue
        for problem in session.check(path):
            problems.append(
                f"{display_path(path)}:"
                f"{problem.line}:{problem.column}: {problem.message}\n"
                f"{render(problem, staged[path])}"
            )

    diff_text = "".join(diff for _, diff in unified_diffs(staged, context=opts.context))

    # dry-runs store the patch so an id alone can apply it later;
    # applies store the reverse so an id alone can revert. What is
    # shown is for reading; what is stored is git-canonical and
    # replays through libgit2.
    stored_text = replayable_patch(staged, context=opts.context)
    dry_run_id = (
        store.save(stored_text) if staged and not opts.apply and store_ids else None
    )

    undo_id = None
    if opts.apply and (not problems or opts.force):
        undo_id = _prepare_undo(staged, opts.context)

    # an apply of known-broken syntax refuses to write unless
    # --force, in which case the problems are on record anyway
    refused = bool(opts.apply and problems and not opts.force)
    applied = False
    if opts.apply and not refused:
        session.apply(staged)
        applied = True

    return Result(
        diff=diff_text,
        files=[display_path(p) for p in sorted(staged)],
        dry_run_id=dry_run_id,
        undo_id=undo_id,
        problems=problems,
        formatted=formatted,
        actions=actions,
        applied=applied,
        refused=refused,
    )


def _prepare_undo(staged: dict[Path, str | bytes | None], context: int) -> str | None:
    """Render the reverse of what is about to be applied, and store it.

    The undo diff must be rendered before the staged state reaches the
    disk: its old side is the post-apply state, its new side the
    pre-apply disk truth. It is stored and never printed, so binary
    changes carry a real payload here and undo like any other change.
    """
    from pyedit import store
    from pyedit.diff import original, replayable_patch

    pre = {path: original(path) for path in staged}
    if not pre:
        return None
    undo_text = replayable_patch(pre, context=context, base=staged)
    if not undo_text:
        return None
    return store.save(undo_text)


def run_revision(opts: Options, revision: str, stage) -> Result:
    """Run stage against REV's tree, amending it back on apply.

    Reads come from the revision (exported to a scratch tree), so the
    diff shows REV to new with no session changes. --apply rewrites
    REV in place through jjrev (the oplog keeps every step
    reversible); without it nothing is stored or written. `stage`
    runs a script or replays a stored patch against the session.
    """
    from pyedit import jjrev

    inv_root = (opts.root or Path.cwd()).resolve()
    repo_root = jjrev.find_repo_root(inv_root)
    target_hex, wc_hex = jjrev.resolve_ids(repo_root, revision)
    if target_hex == wc_hex:
        session = new_session(opts)
        stage(session)
        result = finish(session, opts, allow_meta=True)
        if session.meta:
            old = jjrev.target_meta(repo_root, revision)
            new = _prune_meta(session.meta, old)
            result.meta = {k: (old[k], v) for k, v in new.items()}
            if opts.apply and new and not result.refused:
                result.op = jjrev.retitle(repo_root, revision, new)
                result.applied = True
        return result
    prefix = inv_root.relative_to(repo_root)
    sub = "" if str(prefix) == "." else prefix.as_posix()

    import os
    import tempfile

    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="pyedit-r") as tmp:
        tmproot = Path(tmp)
        jjrev.export_tree(repo_root, revision, tmproot)
        session = EditSession(
            max_bytes=opts.max_bytes,
            max_files=opts.max_files,
            respect_gitignore=opts.respect_gitignore,
            find_renames=opts.find_renames,
            root=tmproot / sub if sub else tmproot,
        )
        # displays resolve against the cwd, and relative subprocess
        # paths with it: follow the session root the way a normal run
        # sits on the invocation root
        os.chdir(session.root)
        try:
            stage(session)
            inner = replace(opts, apply=False)
            result = finish(
                session, inner, config_root=inv_root, store_ids=False,
                allow_meta=True,
            )
        finally:
            os.chdir(previous_cwd)
        if result.problems and not opts.force:
            result.refused = True
            return result
        if not opts.apply:
            if session.meta:
                old = jjrev.target_meta(repo_root, revision)
                new = _prune_meta(session.meta, old)
                result.meta = {k: (old[k], v) for k, v in new.items()}
            return result
        repo_changes = {}
        for p, content in session.staged().items():
            rel = p.relative_to(session.root) if p.is_absolute() else p
            key = f"{sub}/{rel.as_posix()}" if sub else rel.as_posix()
            repo_changes[key] = content
        old = jjrev.target_meta(repo_root, revision)
        new = _prune_meta(session.meta, old)
        result.meta = {k: (old[k], v) for k, v in new.items()}
        if not repo_changes and not new:
            return result
        _new_id, op, cleanup_op, conflicts = jjrev.amend_commit(
            repo_root, revision, repo_changes, new or None
        )
        result.applied = True
        result.op = op
        result.cleanup_op = cleanup_op
        result.conflicts = conflicts
        return result


def _prune_meta(staged: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    """Staged metadata minus values the commit already holds: a
    reword to the same message rewrites nothing."""
    return {k: v for k, v in staged.items() if old.get(k) != v}
