"""LSP-grade semantic editing, in-process.

Rope (the refactoring engine behind pylsp's rename) provides the Python
semantics: import-aware reference finding, project-wide renames
(including keyword arguments and import statements) and module renames
performed as file moves. Rope reads project files through the stdlib,
so during a script run the VFS patches apply and it sees staged
content. Rope never touches disk itself: the project is opened with
ropefolder=None and changes are staged into the session instead of
performed.

Operations are position-based, like textDocument/rename and
textDocument/references: the caller points at the symbol.
"""

from __future__ import annotations

import keyword
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rope.base import change as rope_change
from rope.base.exceptions import RopeError
from rope.base.project import Project
from rope.contrib.findit import find_occurrences
from rope.refactor.rename import Rename

from pyedit import vfs


@dataclass
class Reference:
    path: Path
    line: int  # 1-based
    column: int  # 0-based


def rename_symbol(
    session, path: str | Path, line: int, column: int, old_name: str, new_name: str
) -> list[Path]:
    """Rename the symbol at (line, column) project-wide.

    `old_name` must match what the position resolves to, so a cursor
    slightly off the target fails loudly instead of renaming the wrong
    thing. Stages every file rope would change; returns changed paths.
    """
    _check_identifier("old", old_name)
    _check_identifier("new", new_name)
    with _roped(session):
        content = session.read(path)
        if not isinstance(content, str):
            raise ValueError(f"{session.canon(path)} is binary; rename works on text")
        offset = _offset(content, line, column)
        project = _project(session)
        try:
            resource = _resource_or_none(project, session, path)
            if resource is None:
                raise ValueError(
                    f"{session.relpath(session.canon(path))} is outside the project root"
                )
            try:
                renamer = Rename(project, resource, offset)
                _require_selection(renamer, old_name, session, path, line, column)
                changes = renamer.get_changes(
                    new_name, resources=_python_resources(session, project)
                )
            except RopeError as err:
                raise ValueError(
                    f"no symbol to rename at {session.relpath(session.canon(path))}:"
                    f"{line}:{column}: {err}"
                ) from None
            return _stage_changes(session, changes)
        finally:
            project.close()


def rename_module(session, path: str | Path, old_name: str, new_name: str) -> list[Path]:
    """Rename a module file or package folder and update its importers."""
    _check_identifier("old", old_name)
    _check_identifier("new", new_name)
    with _roped(session):
        project = _project(session)
        try:
            resource = _resource_or_none(project, session, path)
            if resource is None:
                raise ValueError(
                    f"{session.relpath(session.canon(path))} is outside the project root"
                )
            try:
                renamer = Rename(project, resource)
                _require_selection(renamer, old_name, session, path)
                changes = renamer.get_changes(
                    new_name, resources=_python_resources(session, project)
                )
            except RopeError as err:
                raise ValueError(
                    f"cannot rename module {session.relpath(session.canon(path))}: {err}"
                ) from None
            return _stage_changes(session, changes)
        finally:
            project.close()


def references(
    session, path: str | Path, line: int, column: int, name: str
) -> list[Reference]:
    """Every occurrence of the symbol at (line, column).

    `name` must match the identifier at the position.
    """
    _check_identifier("name", name)
    with _roped(session):
        content = session.read(path)
        if not isinstance(content, str):
            raise ValueError(f"{session.canon(path)} is binary; references work on text")
        _require_token(content, line, column, name)
        offset = _offset(content, line, column)
        project = _project(session)
        try:
            resource = _resource_or_none(project, session, path)
            if resource is None:
                raise ValueError(
                    f"{session.relpath(session.canon(path))} is outside the project root"
                )
            try:
                locations = find_occurrences(
                    project, resource, offset, resources=_python_resources(session, project)
                )
            except RopeError as err:
                raise ValueError(
                    f"no symbol at {session.relpath(session.canon(path))}:{line}:{column}: {err}"
                ) from None
            result: list[Reference] = []
            for location in locations:
                file_path = Path(location.resource.real_path)
                file_content = session.read(file_path)
                if not isinstance(file_content, str):
                    continue
                ref_line = file_content.count("\n", 0, location.offset) + 1
                ref_column = (
                    location.offset - file_content.rfind("\n", 0, location.offset) - 1
                )
                result.append(
                    Reference(path=file_path, line=ref_line, column=ref_column)
                )
            return result
        finally:
            project.close()


@contextmanager
def _roped(session):
    """Run rope with the VFS patches active, even outside a script run,
    so it reads staged content like any other file consumer."""
    restore = vfs.install(session)
    try:
        yield
    finally:
        restore()


def _python_resources(session, project: Project) -> list:
    resources = []
    for path in session.glob("**/*.py"):
        try:
            resources.append(project.get_resource(session.relpath(path)))
        except Exception:
            continue
    return resources


def _require_selection(renamer, old_name, session, path, line=None, column=None):
    resolved = renamer.get_old_name()
    if resolved != old_name:
        where = f"{session.relpath(session.canon(path))}"
        if line is not None:
            where += f":{line}:{column}"
        raise ValueError(
            f"selection at {where} resolves to {resolved!r}, not {old_name!r}"
        )


def _require_token(content: str, line: int, column: int, name: str) -> None:
    line_text = content.split("\n")[line - 1]
    if column < len(line_text) and _is_word_char(line_text, column):
        at = column
    elif column > 0 and _is_word_char(line_text, column - 1):
        at = column - 1
    else:
        at = None
    token = None
    if at is not None:
        for match in re.finditer(r"\w+", line_text):
            if match.start() <= at < match.end():
                token = match.group()
                break
    if token != name:
        found = token if token is not None else line_text[column : column + 1] or "nothing"
        raise ValueError(
            f"symbol at line {line}, column {column} is {found!r}, not {name!r}"
        )


def _check_identifier(role: str, name: str) -> None:
    # keywords are lexically valid identifiers but not renameable targets
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"{role} name is not a python identifier: {name!r}")


def _offset(content: str, line: int, column: int) -> int:
    if line < 1:
        raise ValueError(f"line is 1-based, got {line}")
    if column < 0:
        raise ValueError(f"column is 0-based, got {column}")
    lines = content.split("\n")
    if line > len(lines):
        raise ValueError(f"line {line} is past the end of the file ({len(lines)} lines)")
    line_text = lines[line - 1]
    if column > len(line_text):
        raise ValueError(
            f"column {column} is past the end of line {line} ({len(line_text)} characters)"
        )
    offset = sum(len(part) + 1 for part in lines[: line - 1]) + column
    # accept a cursor sitting just after a token, like editors do
    if not _is_word_char(line_text, column):
        if column > 0 and _is_word_char(line_text, column - 1):
            offset -= 1
    return offset


def _is_word_char(line_text: str, column: int) -> bool:
    return line_text[column].isidentifier()


def _project(session) -> Project:
    return Project(session.root.as_posix(), ropefolder=None)


def _resource_or_none(project: Project, session, path: str | Path):
    try:
        return project.get_resource(session.relpath(session.canon(path)))
    except Exception:
        return None


def _stage_changes(session, changes) -> list[Path]:
    staged: list[Path] = []
    for change in changes.changes:
        if isinstance(change, rope_change.ChangeContents):
            path = Path(change.resource.real_path)
            session.write(path, change.new_contents)
            staged.append(path)
        elif isinstance(change, rope_change.MoveResource):
            staged.extend(_stage_move(session, change))
        else:
            raise ValueError(f"unsupported rope change: {type(change).__name__}")
    return staged


def _stage_move(session, change: rope_change.MoveResource) -> list[Path]:
    old_root = Path(change.resource.real_path)
    new_root = Path(change.new_resource.real_path)
    staged: list[Path] = []
    if change.resource.is_folder():
        for file_path in _files_under(session, old_root):
            target = new_root / file_path.relative_to(old_root)
            session.rename(file_path, target)
            staged.append(target)
        return staged
    session.rename(old_root, new_root)
    return [new_root]


def _files_under(session, folder: Path) -> list[Path]:
    found: set[Path] = set()
    for root, _dirs, files in os.walk(folder):
        for name in files:
            found.add(Path(root) / name)
    for staged_path, content in session.staged().items():
        if content is not None and staged_path.is_relative_to(folder) and staged_path.is_file():
            found.add(staged_path)
    return sorted(found)
