"""OpenAI apply_patch (V4A) support.

The envelope (``*** Begin Patch`` / Add / Update / Delete / Move to)
parser is adapted from openai-agents-python (MIT, Copyright (c) 2025
OpenAI); the per-file diff application is the vendored copy in
``pyedit.vendor.apply_diff``. Operations stage into the edit session,
so a patch is a dry-run diff like any other input.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyedit.vendor.apply_diff import apply_diff

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File: "
DELETE_FILE = "*** Delete File: "
UPDATE_FILE = "*** Update File: "
MOVE_TO = "*** Move to: "
FILE_OPERATION_PREFIXES = (ADD_FILE, DELETE_FILE, UPDATE_FILE)


@dataclass
class PatchOperation:
    type: str
    path: str
    diff: str | None = None
    move_to: str | None = None


def parse_patch(text: str) -> list[PatchOperation]:
    lines = text.splitlines()
    if not lines or lines[0] != BEGIN_PATCH:
        raise ValueError("patch must start with '*** Begin Patch'")
    if len(lines) < 2 or lines[-1] != END_PATCH:
        raise ValueError("patch must end with '*** End Patch'")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith(ADD_FILE):
            operation, index = _parse_add_file(lines, index)
        elif line.startswith(DELETE_FILE):
            operation, index = _parse_delete_file(lines, index)
        elif line.startswith(UPDATE_FILE):
            operation, index = _parse_update_file(lines, index)
        else:
            raise ValueError(f"invalid patch file operation header: {line}")
        operations.append(operation)

    if not operations:
        raise ValueError("patch must include at least one file operation")
    return operations


def apply_patch(session, text: str) -> list[PatchOperation]:
    """Parse a patch envelope and stage every operation on the session."""
    operations = parse_patch(text)
    for operation in operations:
        if operation.type == "create_file":
            session.write(operation.path, apply_diff("", operation.diff or "", mode="create"))
        elif operation.type == "update_file":
            _stage_update(session, operation)
        elif operation.type == "delete_file":
            session.delete(operation.path)
        else:
            raise ValueError(f"unknown patch operation type: {operation.type}")
    return operations


def _stage_update(session, operation: PatchOperation) -> None:
    content = session.read(operation.path)
    if isinstance(content, bytes):
        raise ValueError(f"{operation.path} is binary; V4A patches apply to text only")
    patched = apply_diff(content, operation.diff or "")
    if operation.move_to:
        session.rename(operation.path, operation.move_to)
        session.write(operation.move_to, patched)
    else:
        session.write(operation.path, patched)


def _parse_add_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    path = _parse_path_header(lines[index], ADD_FILE)
    index += 1
    diff_lines: list[str] = []
    while index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        line = lines[index]
        if not line.startswith("+"):
            raise ValueError(f"invalid Add File line: {line}")
        diff_lines.append(line)
        index += 1
    if not diff_lines:
        raise ValueError(f"Add File patch for {path} must include at least one + line")
    return PatchOperation(type="create_file", path=path, diff=_join_diff(diff_lines)), index


def _parse_delete_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    path = _parse_path_header(lines[index], DELETE_FILE)
    index += 1
    if index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        raise ValueError(f"Delete File patch for {path} must not include a diff")
    return PatchOperation(type="delete_file", path=path), index


def _parse_update_file(lines: list[str], index: int) -> tuple[PatchOperation, int]:
    path = _parse_path_header(lines[index], UPDATE_FILE)
    index += 1
    move_to = None
    if index < len(lines) - 1 and lines[index].startswith(MOVE_TO):
        move_to = _parse_path_header(lines[index], MOVE_TO)
        index += 1

    diff_lines: list[str] = []
    while index < len(lines) - 1 and not _is_file_operation_header(lines[index]):
        diff_lines.append(lines[index])
        index += 1
    if not diff_lines:
        raise ValueError(f"Update File patch for {path} must include a hunk")
    return (
        PatchOperation(type="update_file", path=path, diff=_join_diff(diff_lines), move_to=move_to),
        index,
    )


def _parse_path_header(line: str, prefix: str) -> str:
    path = line.removeprefix(prefix).strip()
    if not path:
        raise ValueError(f"missing path in patch header: {line}")
    return path


def _is_file_operation_header(line: str) -> bool:
    return line.startswith(FILE_OPERATION_PREFIXES)


def _join_diff(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"
