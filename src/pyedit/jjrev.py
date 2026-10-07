"""Edit the tree at a jj revision, amend it back.

`pyedit -r REV` reads base file content from REV's committed tree
instead of the working copy, and --apply rewrites REV in place with
the staged bytes exactly: descendants rebase (conflicts they gain
are kept as markers for the next round) and the oplog keeps every
step reversible. A squash would merge tip content into the target
instead, so the amend restores exact bytes and reports the commits
that carry markers. Resolve markers where they are first created,
not at the tip.

`pyedit.describe()` and `pyedit.author()` stage the commit's
message and author next to the tree; both land in the same
transaction as the tree restore, so one op holds the whole amend.

pyjj is a soft dependency: it is simply absent when the distributor
leaves it out of the runtime closure, and only -r needs it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pyedit.session import Symlink, _parse_author

try:
    import pyjj
except ImportError:
    pyjj = None


class JjRevError(Exception):
    """A -r run cannot proceed: no pyjj, no repo, an unknown
    revision, a dirty working copy, or an unreadable tree entry.
    Nothing was written."""


def have_pyjj() -> bool:
    """Whether pyjj made it into the runtime closure."""
    return pyjj is not None


def require_pyjj():
    """The pyjj module, or a loud refusal naming the closure."""
    if pyjj is None:
        raise JjRevError(
            "pyjj is not in the runtime closure; -r needs it, "
            "ask the distributor to include it"
        )
    return pyjj


def find_repo_root(start: Path) -> Path:
    """The workspace root containing `start`; loud when there is none."""
    require_pyjj()
    try:
        return Path(pyjj.open(str(start)).root).resolve()
    except Exception as err:
        raise JjRevError(f"no jj repository contains {start}: {err}") from err


def resolve_ids(repo_root: Path, rev: str) -> tuple[str, str]:
    """(target commit id, working-copy commit id) for `rev`.

    Anything but exactly one commit is the caller's mistake, and
    pyjj's own message says which.
    """
    require_pyjj()
    try:
        repo = pyjj.open(str(repo_root))
        return _hex(repo.resolve(rev)), _hex(repo.resolve("@"))
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot resolve {rev!r}: {err}") from err


def _hex(commit) -> str:
    return commit.id.hex()


def export_tree(repo_root: Path, rev: str, dest: Path) -> list[str]:
    """Materialize REV's whole tree into `dest`, byte-exact.

    Regular files land with their bytes and executable bit, symlinks
    relink. Anything the tree names that reads as nothing (submodules,
    conflicted paths) fails the export naming it: amending those back
    is not implemented. Returns repo-relative posix paths exported.
    """
    require_pyjj()
    try:
        repo = pyjj.open(str(repo_root))
        target = repo.resolve(rev)
        names = target.list_files(None)
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot read tree at {rev!r}: {err}") from err
    exported = []
    for name in names:
        try:
            data = target.read_file(name)
        except Exception as err:
            raise JjRevError(f"-r {rev}: cannot export {name}: {err}") from err
        dst = dest / Path(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        executable = target.is_executable(name)
        if executable is None:
            # read_file returns a link's target; only links (not
            # files) report no executable bit
            try:
                os.symlink(data.decode("utf-8"), dst)
            except UnicodeDecodeError as err:
                raise JjRevError(
                    f"-r {rev}: link target at {name} is not utf-8"
                ) from err
        else:
            dst.write_bytes(data)
            if executable:
                dst.chmod(0o755)
        exported.append(name)
    return exported


def _tree_state_opt(commit, rel: str) -> tuple:
    """_tree_state, with absence as a value instead of a refusal."""
    try:
        return _tree_state(commit, rel)
    except JjRevError:
        return ("absent", None)


def _matches_saved(commit, saved: dict[str, tuple]) -> bool:
    """Every touched path in the commit holds its pre-run content."""
    for rel, (kind, payload, _mode) in saved.items():
        if _tree_state_opt(commit, rel) != (kind, payload):
            return False
    return True


def _matches_staged(commit, changes: dict[str, object]) -> bool:
    """Every touched path in the commit holds the staged bytes."""
    for rel, new in changes.items():
        if isinstance(new, Symlink):
            want = ("link", str(new))
        elif new is None:
            want = ("absent", None)
        else:
            want = (
                "file",
                new.encode("utf-8") if isinstance(new, str) else bytes(new),
            )
        if _tree_state_opt(commit, rel) != want:
            return False
    return True


def _conflict_names(repo) -> list[str]:
    """Short id plus subject for every commit carrying markers."""
    names = []
    for commit in repo.conflicts():
        subject = (commit.description or "").splitlines()
        names.append(
            f"{commit.id.hex()[:12]} {subject[0] if subject else '(no description)'}"
        )
    return names


def amend_commit(
    repo_root: Path,
    rev: str,
    changes: dict[str, object],
    meta: dict[str, str] | None = None,
) -> tuple[str, str, str | None, list[str]]:
    """Rewrite REV with new contents; return (new id, op, cleanup, conflicts).

    `changes` maps repo-relative posix paths to staged values: bytes
    or str for file content, None for deletion, a Symlink for a new
    link target. File changes, additions, deletions and link
    retargets are covered; flips between files and links are refused.

    `meta` stages commit metadata next to the tree: `description`
    and/or `author` ("Name <email>", timestamp kept). Both land in
    the same transaction as the tree restore, so one op holds the
    whole amend and its restore undoes both together.

    The working copy carries the new content just long enough for the
    entry snapshot to take it into @; two restores then move exact
    bytes, never a merge: first the staged bytes into REV (a squash
    would fold divergent tip content in instead), then the touched
    paths in @ back to what they held, so the copy is clean again.
    The second restore runs only when the rebase left @ holding the
    staged bytes; when it left markers or descendant content there,
    the files stay exactly as checkout wrote them. `cleanup` names
    the second transaction when one ran; `conflicts` names the
    commits that carry markers, to resolve where they were created.
    The working copy must start clean on every touched path, or the
    run refuses naming them.
    """
    require_pyjj()
    meta = dict(meta or {})
    if not changes and not meta:
        raise JjRevError("nothing to amend")
    try:
        repo = pyjj.open(str(repo_root))
        target = repo.resolve(rev)
        at = repo.resolve("@")
        target_files = set(target.list_files(None))
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot resolve {rev!r}: {err}") from err

    saved = _check_clean(repo_root, at, changes)
    for rel, new in changes.items():
        refusal = _refuse_flip(target_files, target, rel, saved[rel], new)
        if refusal is not None:
            raise JjRevError(refusal)

    wc_hex = _hex(at)
    if wc_hex == _hex(target):
        raise JjRevError(f"-r {rev} names the working copy itself; run without -r")
    for rel, new in changes.items():
        _write_wc(repo_root, target, rel, new)
    try:
        with repo.atomic(f"pyedit -r {rev}", allow_conflicts=True) as tx:
            if changes:
                written = tx.restore(sorted(changes), into=rev, from_revision="@")
            else:
                # metadata-only: the pre-block commit is the state the
                # transaction started from, which is what a rewrite
                # takes
                written = repo.resolve(rev)
            written = _apply_meta(repo, tx, written, meta)
        op = repo.operation_id
    except Exception as err:
        raise JjRevError(f"amending {rev!r} failed: {err}") from err
    cleanup = _reconcile_wc(repo_root, repo, rev, op, wc_hex, saved, changes)
    return _hex(written), op, cleanup, _conflict_names(repo)


def create_after(
    repo_root: Path, rev: str, message: str, changes: dict[str, object]
) -> tuple[str, str, str | None, list[str]]:
    """A new commit after REV carrying `changes`; return (id, op, cleanup,
    conflicts). REV's children reparent onto it, so the stack keeps its
    shape with the new commit in the middle.

    The tree starts as REV's whole tree with the staged values over
    it, exact bytes through the same vehicle the amend uses (the
    working copy lends its files to the entry snapshot, then goes
    back to what it held). An empty scope refuses: a commit that
    carries no edit is a `jj new` the caller should ask for plainly.
    A conflicted REV refuses first: whole-tree copies carry markers
    nowhere. Rewriting a public child to reparent it refuses through
    jj's own guard.
    """
    require_pyjj()
    if not isinstance(message, str):
        raise JjRevError(f"commit message needs str, got {type(message).__name__}")
    if not changes:
        raise JjRevError("nothing to commit: the scope staged no changes")
    try:
        repo = pyjj.open(str(repo_root))
        base = repo.resolve(rev)
        at = repo.resolve("@")
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot resolve {rev!r}: {err}") from err
    if base.has_conflict:
        raise JjRevError(
            f"{rev} carries conflicts; resolve them there before "
            "committing on top of it, nothing was written"
        )
    if _hex(base) == _hex(at):
        raise JjRevError(
            f"cannot commit after {rev}: it names the working-copy "
            "commit itself, whose half-snapshotted state no insert "
            "builds on; describe it and advance, or pick @-, nothing "
            "was written"
        )

    saved = _check_clean(repo_root, at, changes)
    for rel, new in changes.items():
        _write_wc(repo_root, base, rel, new)
    # The cleanup restores the working copy from the commit holding
    # the pre-run content, which only the pre-snapshot id names: the
    # snapshot below folds the vehicle into @ under a new id.
    wc_hex = _hex(at)
    # Fold the vehicle before resolving anything the block
    # rewrites: the entry snapshot replaces @ when dirty, so
    # pre-snapshot objects (notably @ itself as a child) would
    # address a commit the block no longer holds. The block's own
    # entry snapshot is then a clean no-op.
    repo.snapshot()
    try:
        base = repo.resolve(rev)
        at = repo.resolve("@")
        kids = repo.revset(f"{_hex(base)}+")
    except Exception as err:
        raise JjRevError(f"cannot resolve {rev!r}: {err}") from err
    base_hex = _hex(base)
    # whether the surgery moves @ itself: its rebased content then
    # carries the change through history, and no cleanup may lift
    # it back out as leftover vehicle
    reparented = _hex(at) in {kid.id.hex() for kid in kids}
    try:
        with repo.atomic(f"pyedit commit after {rev}", allow_conflicts=True) as tx:
            new = tx.new([rev], message=message, edit=False)
            new = tx.restore(None, into=new, from_revision=rev)
            new = tx.restore(sorted(changes), into=new, from_revision="@")
            _reparent(tx, repo, base_hex, new, kids)
        op = repo.operation_id
    except Exception as err:
        raise JjRevError(f"committing after {rev!r} failed: {err}") from err
    cleanup = _reconcile_wc(
        repo_root, repo, rev, op, wc_hex, saved, changes, reparented
    )
    return _hex(new), op, cleanup, _conflict_names(repo)


def _check_clean(repo_root: Path, at, changes: dict[str, object]) -> dict[str, tuple]:
    """The working copy holds no uncommitted state on touched paths.

    Returns what each path held, for the reconcile to put back. The
    vehicle borrows these files, so anything else there refuses
    naming the paths; nothing was written.
    """
    at_files = set(at.list_files(None))
    saved: dict[str, tuple] = {}
    dirty: list[str] = []
    for rel in changes:
        old = _wc_state(repo_root / rel)
        if rel in at_files:
            try:
                want = _tree_state(at, rel)
            except JjRevError:
                dirty.append(f"{rel} (unreadable in @)")
                continue
            if (old[0], old[1]) != (want[0], want[1]):
                dirty.append(rel)
                continue
        elif old[0] != "absent":
            dirty.append(rel)
            continue
        saved[rel] = old
    if dirty:
        raise JjRevError(
            "working copy has uncommitted changes in "
            + ", ".join(sorted(dirty))
            + "; commit or shelve them first, nothing was written"
        )
    return saved


def _reconcile_wc(
    repo_root: Path,
    repo,
    rev: str,
    op: str,
    wc_hex: str,
    saved: dict[str, tuple],
    changes: dict[str, object],
    reparented: bool = False,
) -> str | None:
    """The working copy after the transaction; return the cleanup op.

    The entry snapshot took the vehicle files into @ and the block
    rebased it: when @ still holds the pre-run content the vehicle
    files go back byte-identical; when it holds the staged bytes a
    second transaction moves them back out; otherwise jj checked out
    rebased descendants itself, and the files stay as checkout wrote
    them, verified against the rebased commit. When the block
    reparented @ itself the staged match means the rebase carried
    the change, not leftover vehicle, so no cleanup lifts it out:
    the files stay as checkout wrote them, verified.
    """
    current = repo.resolve("@")
    if _matches_saved(current, saved):
        _restore_wc(repo_root, saved)
        _verify_wc(repo_root, saved)
        return None
    if _matches_staged(current, changes) and not reparented:
        # the rebased tip still holds the staged bytes: the entry
        # snapshot committed them there and the rebase kept them, so
        # move them back out, leaving the copy as found
        try:
            with repo.atomic(
                f"pyedit working-copy cleanup after {rev}",
                allow_conflicts=True,
            ) as tx:
                tx.restore(sorted(changes), into="@", from_revision=wc_hex)
            cleanup = repo.operation_id
        except Exception as err:
            raise JjRevError(f"cleaning up after {rev!r}: {err}") from err
        _restore_wc(repo_root, saved)
        _verify_wc(repo_root, saved)
        return cleanup
    _verify_rebased(repo_root, current, saved, rev, op)
    return None


def _reparent(tx, repo, base_hex: str, new, kids) -> None:
    """Move REV's children onto the inserted commit, in the open block.

    Each child keeps every parent but REV, which the new commit
    replaces: merges stay merges. `new` is the restore's own return,
    already the transaction's state, so no resolution can land on a
    commit the block replaced. jj's own guard refuses public
    children first; the exit's rebase carries the grandchildren.
    """
    if not kids:
        return
    raw = tx.transaction
    raw.check_rewritable(repo.settings, [kid.id for kid in kids])
    inner = getattr(repo, "_repo", None)
    if inner is None:
        raise JjRevError(
            "pyjj no longer exposes the readonly repo; "
            "reparenting needs a pyjj verb for it"
        )
    new_id = new.id
    for kid in kids:
        parents = [
            new_id if parent.hex() == base_hex else parent for parent in kid.parent_ids
        ]
        raw.rewrite_commit(repo.settings, kid).set_parents(parents).write(inner)


def target_meta(repo_root: Path, rev: str) -> dict[str, str]:
    """The commit's current description and author ("Name <email>").

    The dry-run old side, and what a staged value is pruned against
    when nothing changed.
    """
    require_pyjj()
    try:
        repo = require_pyjj().open(str(repo_root))
        target = repo.resolve(rev)
        return {
            "description": target.description or "",
            "author": _format_author(target.author),
        }
    except Exception as err:
        raise JjRevError(f"cannot read metadata at {rev!r}: {err}") from err


def retitle(repo_root: Path, rev: str, meta: dict[str, str]) -> str:
    """Set description/author on REV with no tree changes; return the op.

    The -r @ short-circuit: describing the working-copy commit
    rewrites no tree, so it is always safe.
    """
    require_pyjj()
    try:
        repo = require_pyjj().open(str(repo_root))
        with repo.atomic(f"pyedit -r {rev} (metadata)", allow_conflicts=True) as tx:
            _apply_meta(repo, tx, repo.resolve(rev), meta)
        return repo.operation_id
    except Exception as err:
        raise JjRevError(f"setting metadata at {rev!r} failed: {err}") from err


def _apply_meta(repo, tx, commit, meta: dict[str, str]):
    """Description then author onto `commit`, inside the open block.

    `describe` takes the commit object itself rather than a revset:
    it is already the transaction's state, so no second resolution
    can land on a commit the block replaced. The author has no
    wrapper verb (a pyjj-side verb is the honest fix), so the raw
    transaction rewrites it through the public builder.
    """
    if "description" in meta:
        commit = tx.describe(commit, message=meta["description"])
    if "author" in meta:
        commit = _rewrite_author(repo, tx, commit, meta["author"])
    return commit


def _rewrite_author(repo, tx, commit, who: str):
    """A new "Name <email>" on `commit`, keeping its timestamp."""
    module = require_pyjj()
    name, email = _split_author(_parse_author(who))
    try:
        stamp = module.Timestamp(
            commit.author.timestamp.millis_since_epoch,
            commit.author.timestamp.tz_offset_minutes,
        )
        builder = tx.transaction.rewrite_commit(repo.settings, commit).set_author(
            module.Signature(name, email, stamp)
        )
        inner = getattr(repo, "_repo", None)
        if inner is None:
            raise JjRevError(
                "pyjj no longer exposes the readonly repo; "
                "the author rewrite needs a pyjj author verb"
            )
        return builder.write(inner)
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot set author: {err}") from err


def _format_author(sig) -> str:
    """A signature as the "Name <email>" that stages it back."""
    return f"{sig.name} <{sig.email}>"


def _split_author(who: str) -> tuple[str, str]:
    """The name and email out of normalized "Name <email>"."""
    name, email = who.rsplit("<", 1)
    return name.strip(), email[:-1].strip()


def _wc_state(path: Path) -> tuple:
    """(kind, payload, mode) of a working-copy path: kind is file,
    link or absent; payload is bytes or a link target; mode holds the
    permission bits for restore."""
    if not os.path.lexists(path):
        return ("absent", None, None)
    if os.path.islink(path):
        return ("link", os.readlink(path), None)
    if path.is_dir():
        raise JjRevError(f"{path} is a directory in the working copy")
    st = path.stat()
    return ("file", path.read_bytes(), stat.S_IMODE(st.st_mode))


def _tree_state(commit, rel: str) -> tuple:
    """(kind, payload) of a committed path: file bytes or a link
    target. Anything else is unreadable, said loudly."""
    try:
        data = commit.read_file(rel)
    except Exception as err:
        raise JjRevError(f"cannot read {rel}: {err}") from err
    if commit.is_executable(rel) is None:
        return ("link", data.decode("utf-8"))
    return ("file", data)


def _refuse_flip(target_files: set, target, rel, old, new) -> str | None:
    """Why a change touching a symlink cannot amend, or None when the
    kinds line up: files, additions, deletions and link retargets."""
    new_kind = (
        "link" if isinstance(new, Symlink) else "absent" if new is None else "file"
    )
    if old[0] == "absent" or new_kind == "absent":
        return None
    if old[0] == "link" and new_kind == "link":
        return None
    if old[0] == "file" and new_kind == "file":
        if rel in target_files and target.is_executable(rel) is None:
            return (
                f"{rel} is a symlink in the revision; -r amends file "
                "content, not link flips"
            )
        return None
    return (
        f"{rel} changes a symlink; -r covers file content, "
        "additions, deletions and link retargets"
    )


def _write_wc(repo_root, target, rel, new) -> None:
    """Lay new content into the working copy, matching REV's modes."""
    path = repo_root / rel
    if new is None:
        if os.path.islink(path):
            os.unlink(path)
        else:
            path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(new, Symlink):
        if os.path.lexists(path):
            if os.path.islink(path):
                os.unlink(path)
            else:
                path.unlink()
        os.symlink(str(new), path)
        return
    data = new.encode("utf-8") if isinstance(new, str) else bytes(new)
    path.write_bytes(data)
    if target.is_executable(rel):
        path.chmod(0o755)
    else:
        path.chmod(0o644)


def _restore_wc(repo_root: Path, saved: dict[str, tuple]) -> None:
    """Put the working copy back byte- and mode-identical."""
    for rel, (kind, payload, mode) in saved.items():
        path = repo_root / rel
        if kind == "absent":
            if os.path.islink(path):
                os.unlink(path)
            else:
                path.unlink(missing_ok=True)
        elif kind == "link":
            if os.path.lexists(path):
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(payload, path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if mode is not None:
                os.chmod(path, mode)


def _verify_rebased(repo_root, at_commit, saved, rev, op) -> None:
    """The working copy after jj checked out the rebased descendants.

    Our squash moved @, so jj wrote its new tree out itself: every
    touched path must already match it. Restoring our saved bytes
    here would dirty the copy, so a mismatch names itself with the op
    that holds the amend instead of passing silently.
    """
    try:
        at_files = set(at_commit.list_files(None))
    except Exception as err:
        raise JjRevError(f"cannot list rebased @: {err}") from err
    for rel in saved:
        now = _wc_state(repo_root / rel)
        if rel in at_files:
            try:
                want = _tree_state(at_commit, rel)
            except JjRevError:
                raise JjRevError(
                    f"working copy at {rel} is unreadable in rebased @; "
                    f"the amend stands at op {op}, restore one side"
                ) from None
        else:
            want = ("absent", None)
        if (now[0], now[1]) != (want[0], want[1]):
            raise JjRevError(
                f"working copy at {rel} does not match rebased @; "
                f"the amend stands at op {op}, restore one side"
            )


def _verify_wc(repo_root: Path, saved: dict[str, tuple]) -> None:
    """The restore really landed; a crash or concurrent edit in the
    window names itself instead of passing silently."""
    for rel, (kind, payload, mode) in saved.items():
        now = _wc_state(repo_root / rel)
        if (now[0], now[1]) != (kind, payload) or (mode is not None and now[2] != mode):
            raise JjRevError(
                f"working copy at {rel} no longer matches; the amend "
                "stands in history, restore one side before continuing"
            )
