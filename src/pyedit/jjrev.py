"""Edit the tree at a jj revision, amend it back.

`pyedit -r REV` reads base file content from REV's committed tree
instead of the working copy, and --apply rewrites REV in place with
the staged bytes exactly: descendants rebase (conflicts they gain
are kept as markers for the next round) and the oplog keeps every
step reversible. A squash would merge tip content into the target
instead, so the amend restores exact bytes and reports the commits
that carry markers. Resolve markers where they are first created,
not at the tip.

pyjj is a soft dependency: it is simply absent when the distributor
leaves it out of the runtime closure, and only -r needs it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pyedit.session import Symlink

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
    repo_root: Path, rev: str, changes: dict[str, object]
) -> tuple[str, str, str | None, list[str]]:
    """Rewrite REV with new contents; return (new id, op, cleanup, conflicts).

    `changes` maps repo-relative posix paths to staged values: bytes
    or str for file content, None for deletion, a Symlink for a new
    link target. File changes, additions, deletions and link
    retargets are covered; flips between files and links are refused.

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
    if not changes:
        raise JjRevError("nothing to amend")
    try:
        repo = pyjj.open(str(repo_root))
        target = repo.resolve(rev)
        at = repo.resolve("@")
        at_files = set(at.list_files(None))
        target_files = set(target.list_files(None))
    except JjRevError:
        raise
    except Exception as err:
        raise JjRevError(f"cannot resolve {rev!r}: {err}") from err

    saved: dict[str, tuple] = {}
    dirty: list[str] = []
    for rel, new in changes.items():
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
        refusal = _refuse_flip(target_files, target, rel, old, new)
        if refusal is not None:
            raise JjRevError(refusal)
        saved[rel] = old
    if dirty:
        raise JjRevError(
            "working copy has uncommitted changes in "
            + ", ".join(sorted(dirty))
            + "; commit or shelve them first, nothing was written"
        )

    wc_hex = _hex(at)
    if wc_hex == _hex(target):
        raise JjRevError(f"-r {rev} names the working copy itself; run without -r")
    for rel, new in changes.items():
        _write_wc(repo_root, target, rel, new)
    try:
        with repo.atomic(f"pyedit -r {rev}", allow_conflicts=True) as tx:
            written = tx.restore(sorted(changes), into=rev, from_revision="@")
        op = repo.operation_id
    except Exception as err:
        raise JjRevError(f"amending {rev!r} failed: {err}") from err
    current = repo.resolve("@")
    cleanup = None
    if _matches_saved(current, saved):
        _restore_wc(repo_root, saved)
        _verify_wc(repo_root, saved)
    elif _matches_staged(current, changes):
        # the rebased tip still holds the staged bytes: the entry
        # snapshot committed them there and the rebase kept them, so
        # move them back out, leaving the copy as found
        try:
            with repo.atomic(
                f"pyedit -r {rev} (working-copy cleanup)",
                allow_conflicts=True,
            ) as tx:
                tx.restore(sorted(changes), into="@", from_revision=wc_hex)
            cleanup = repo.operation_id
        except Exception as err:
            raise JjRevError(f"cleaning up after {rev!r}: {err}") from err
        _restore_wc(repo_root, saved)
        _verify_wc(repo_root, saved)
    else:
        _verify_rebased(repo_root, current, saved, rev, op)
    return _hex(written), op, cleanup, _conflict_names(repo)


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
