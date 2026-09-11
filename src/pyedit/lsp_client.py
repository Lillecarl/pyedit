"""Edit any language through its language server.

The server binary comes from the environment as a command list, never
downloaded: pyedit speaks LSP over stdio through pygls's client, the
same machinery behind pytest-lsp. Servers see the session overlay
through didOpen/didChange -- staged content is the document the server
computes over, unstaged files stay disk truth. A returned WorkspaceEdit
is staged into the session like any other edit, so the dry-run diff
shows exactly what the server would have done.

LSP positions are unit-based and the encoding is negotiated: utf-8 is
requested, servers that only offer utf-16 get their columns converted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from lsprotocol import types
from pygls.io_ import run_async
from pygls.lsp.client import LanguageClient as _LanguageClient

from pyedit.rope import Reference, _require_token
from pyedit.session import EditSession
from pyedit._version import __version__

logger = logging.getLogger(__name__)


_LANGUAGE_IDS = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".nix": "nix",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".lua": "lua",
    ".zig": "zig",
    ".json": "json",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


def _language_id(path: Path) -> str:
    return _LANGUAGE_IDS.get(path.suffix.lower(), "plaintext")


def _uri(path: Path) -> str:
    return path.as_uri()


def _path(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


def _units(line_text: str, column: int, encoding: str) -> int:
    """A character column as the encoding's position units."""
    if encoding == "utf-32":
        return column
    prefix = line_text[:column]
    if encoding == "utf-16":
        return len(prefix.encode("utf-16-le")) // 2
    return len(prefix.encode("utf-8"))


def _column(line_text: str, units: int, encoding: str) -> int:
    """Position units back into a character column, floored into the
    character that owns the unit."""
    if encoding == "utf-32":
        return min(units, len(line_text))
    used = 0
    for index, char in enumerate(line_text):
        if encoding == "utf-16":
            width = 1 if ord(char) < 0x10000 else 2
        else:
            width = len(char.encode("utf-8"))
        if used + width > units:
            return index
        used += width
        if used == units:
            return index + 1
    return len(line_text)


class _FeedWriter:
    """A pygls writer that feeds bytes into an asyncio.StreamReader.

    Two of these joined back to back are an in-memory duplex JSON-RPC
    channel: real framing end to end with no subprocess, socket or pipe.
    """

    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target

    def write(self, data: bytes) -> None:
        self._target.feed_data(data)

    def close(self) -> None:
        self._target.feed_eof()


def _offset_at(content: str, position: types.Position, encoding: str) -> int:
    lines = content.split("\n")
    if position.line >= len(lines):
        raise ValueError(
            f"the language server sent a position past the end of the file: {position}"
        )
    line_text = lines[position.line]
    column = min(_column(line_text, position.character, encoding), len(line_text))
    return sum(len(part) + 1 for part in lines[: position.line]) + column


def _apply_edits(
    content: str, edits: list, encoding: str, path: Path
) -> str:
    """Splice TextEdits into one new string, bottom-up so earlier
    offsets stay valid while later ones apply."""
    spans = []
    for edit in edits:
        start = _offset_at(content, edit.range.start, encoding)
        end = _offset_at(content, edit.range.end, encoding)
        if end < start:
            raise ValueError(
                f"the language server sent an inverted edit range for {path}: {edit.range}"
            )
        spans.append((start, end, edit.new_text))
    order = sorted(range(len(spans)), key=lambda index: (spans[index][0], index))
    limit = len(content)
    for index in reversed(order):
        start, end, new_text = spans[index]
        if end > limit:
            raise ValueError(f"the language server sent overlapping edits for {path}")
        content = content[:start] + new_text + content[end:]
        limit = start
    return content


class LspSession:
    """A language server bound to an edit session.

    with pyedit.lsp(["rust-analyzer"]) as lsp:
        lsp.rename_symbol("src/main.rs", 12, 6, "old_name", "new_name")

    The context manager owns the server process and a background event
    loop; operations block until the server answers. Servers are
    environment tools: the command must resolve on PATH or be absolute.
    """

    def __init__(
        self,
        session: EditSession,
        command: list[str],
        timeout: float = 120.0,
        in_memory=None,
    ) -> None:
        self._session = session
        self._command = list(command)
        self._timeout = timeout
        # test seam: an async stub server wired over in-memory streams
        self._in_memory = in_memory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: _LanguageClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._encoding = "utf-16"
        self._pushed: dict[Path, str] = {}
        self._versions: dict[Path, int] = {}
        self._ready: threading.Event | None = None
        self._connect_error: BaseException | None = None
        self._to_server: _FeedWriter | None = None
        self._to_client: _FeedWriter | None = None

    def __enter__(self) -> "LspSession":
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, name="pyedit-lsp", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self._timeout):
            self._kill()
            raise RuntimeError("the language server did not initialize in time")
        if self._connect_error is not None:
            self._kill()
            raise self._connect_error
        return self

    def __exit__(self, *exc) -> None:
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(
                self._timeout
            )
        finally:
            self._kill()

    def _kill(self) -> None:
        """Stop the loop and reap the thread, whatever state they are in."""
        loop, thread = self._loop, self._thread
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(self._timeout)
        if not loop.is_running():
            loop.close()
        self._loop = None

    def rename_symbol(
        self,
        path: str | Path,
        line: int,
        column: int,
        old_name: str,
        new_name: str,
    ) -> list[Path]:
        """Rename the symbol at (line, column) server-wide.

        `old_name` must match the token at the position, so a cursor
        slightly off fails loudly instead of renaming the wrong thing.
        Returns staged paths; the diff shows the coverage.
        """
        return self._call(
            self._rename_symbol(self._session.canon(path), line, column, old_name, new_name)
        )

    def references(
        self, path: str | Path, line: int, column: int, name: str
    ) -> list[Reference]:
        """Every reference to the symbol at (line, column).

        `name` must match the token at the position.
        """
        return self._call(
            self._references(self._session.canon(path), line, column, name)
        )

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout)

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except BaseException as err:  # surfaced through the ready event
            self._connect_error = err
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _connect(self) -> None:
        client = _LanguageClient("pyedit", __version__)
        self._client = client
        if self._in_memory is not None:
            to_server = asyncio.StreamReader()
            to_client = asyncio.StreamReader()
            self._to_server = _FeedWriter(to_server)
            self._to_client = _FeedWriter(to_client)
            client.protocol.set_writer(self._to_server)
            self._tasks.append(
                asyncio.create_task(self._in_memory(to_server, self._to_client))
            )
            self._tasks.append(
                asyncio.create_task(
                    run_async(
                        stop_event=asyncio.Event(),
                        reader=to_client,
                        protocol=client.protocol,
                        logger=logger,
                    )
                )
            )
        else:
            await client.start_io(*self._command)
        result = await asyncio.wait_for(
            client.protocol.send_request_async(
                "initialize",
                types.InitializeParams(
                    capabilities=types.ClientCapabilities(
                        general=types.GeneralClientCapabilities(
                            position_encodings=[types.PositionEncodingKind.Utf8]
                        )
                    ),
                    root_uri=self._session.root.as_uri(),
                    workspace_folders=[
                        types.WorkspaceFolder(
                            uri=self._session.root.as_uri(), name="pyedit"
                        )
                    ],
                ),
            ),
            self._timeout,
        )
        if result.capabilities.position_encoding is not None:
            self._encoding = result.capabilities.position_encoding
        client.protocol.notify("initialized", types.InitializedParams())
        await self._sync_documents()

    async def _shutdown(self) -> None:
        try:
            await asyncio.wait_for(
                self._client.protocol.send_request_async("shutdown", None), 5.0
            )
        except Exception:
            pass
        if self._in_memory is None:
            self._client.protocol.notify("exit", None)
        else:
            # memory mode: there is no process to exit; eof ends the stub
            self._to_server.close()
            self._to_client.close()
        await self._client.stop()
        for task in self._tasks:
            try:
                await task
            except BaseException:  # a dying stub must not wedge teardown
                pass

    async def _sync_documents(self) -> None:
        """Push the session tree so the server computes over the overlay.

        Staged files carry their staged content; untouched files are
        opened with their disk content. Pushing the whole tree up front
        keeps the server from racing its own workspace scan on files we
        are about to ask about.
        """
        staged = self._session.staged()
        paths = set(self._session.glob("**/*"))
        paths.update(staged)
        for path in sorted(paths):
            await self._sync_one(path, staged.get(path))

    async def _sync_one(self, path: Path, staged_content) -> None:
        content = staged_content
        if content is None:
            try:
                content = self._session.read(path)
            except OSError:
                content = None
        protocol = self._client.protocol
        uri = _uri(path)
        if not isinstance(content, str):
            if path in self._pushed:
                protocol.notify(
                    "textDocument/didClose",
                    types.DidCloseTextDocumentParams(
                        text_document=types.TextDocumentIdentifier(uri=uri)
                    ),
                )
                del self._pushed[path]
                self._versions.pop(path, None)
            return
        pushed = self._pushed.get(path)
        if pushed is None:
            version = self._versions.get(path, 0) + 1
            self._versions[path] = version
            protocol.notify(
                "textDocument/didOpen",
                types.DidOpenTextDocumentParams(
                    text_document=types.TextDocumentItem(
                        uri=uri,
                        language_id=_language_id(path),
                        version=version,
                        text=content,
                    )
                ),
            )
            self._pushed[path] = content
        elif pushed != content:
            version = self._versions[path] + 1
            self._versions[path] = version
            protocol.notify(
                "textDocument/didChange",
                types.DidChangeTextDocumentParams(
                    text_document=types.VersionedTextDocumentIdentifier(
                        uri=uri, version=version
                    ),
                    content_changes=[
                        types.TextDocumentContentChangeWholeDocument(text=content)
                    ],
                ),
            )
            self._pushed[path] = content

    async def _rename_symbol(self, path, line, column, old_name, new_name):
        content = self._require_text(path, line, column, old_name)
        await self._sync_documents()
        edit = await self._request(
            "textDocument/rename",
            types.RenameParams(
                text_document=types.TextDocumentIdentifier(uri=_uri(path)),
                position=self._position(content, line, column),
                new_name=new_name,
            ),
        )
        if edit is None:
            raise ValueError(f"the language server cannot rename at {line}:{column}")
        return self._stage_workspace_edit(edit)

    async def _references(self, path, line, column, name):
        content = self._require_text(path, line, column, name)
        await self._sync_documents()
        locations = await self._request(
            "textDocument/references",
            types.ReferenceParams(
                text_document=types.TextDocumentIdentifier(uri=_uri(path)),
                position=self._position(content, line, column),
                context=types.ReferenceContext(include_declaration=True),
            ),
        )
        return self._map_locations(locations or [])

    def _require_text(self, path, line, column, name) -> str:
        content = self._session.read(path)
        if not isinstance(content, str):
            raise ValueError(f"{path} is binary; the language server works on text")
        lines = content.split("\n")
        if line < 1 or line > len(lines):
            raise ValueError(f"line {line} is past the end of the file ({len(lines)} lines)")
        if column < 0 or column > len(lines[line - 1]):
            raise ValueError(
                f"column {column} is past the end of line {line} "
                f"({len(lines[line - 1])} characters)"
            )
        _require_token(content, line, column, name)
        return content

    async def _request(self, method: str, params):
        try:
            return await self._client.protocol.send_request_async(method, params)
        except Exception as err:
            raise ValueError(f"the language server rejected {method}: {err}") from err

    def _position(self, content: str, line: int, column: int) -> types.Position:
        line_text = content.split("\n")[line - 1]
        return types.Position(
            line=line - 1, character=_units(line_text, column, self._encoding)
        )

    def _stage_workspace_edit(self, edit: types.WorkspaceEdit) -> list[Path]:
        documents: dict[Path, list] = {}
        if edit.document_changes:
            for change in edit.document_changes:
                if not isinstance(change, types.TextDocumentEdit):
                    kind = getattr(change, "kind", "unknown")
                    raise ValueError(
                        f"the language server requested a {kind!r} resource "
                        "operation; pyedit stages text edits only"
                    )
                documents.setdefault(_path(change.text_document.uri), []).extend(
                    change.edits
                )
        elif edit.changes:
            for uri, edits in edit.changes.items():
                documents.setdefault(_path(uri), []).extend(edits)
        staged = []
        for path, edits in documents.items():
            content = self._session.read(path)
            if not isinstance(content, str):
                raise ValueError(f"the language server edited {path} but it is not text here")
            self._session.write(path, _apply_edits(content, edits, self._encoding, path))
            staged.append(path)
        return staged

    def _map_locations(self, locations) -> list[Reference]:
        refs = []
        lines_by_path: dict[Path, list[str]] = {}
        for location in locations:
            ref_path = _path(location.uri)
            if ref_path not in lines_by_path:
                content = self._session.read(ref_path)
                if not isinstance(content, str):
                    continue
                lines_by_path[ref_path] = content.split("\n")
            lines = lines_by_path[ref_path]
            line0 = location.range.start.line
            if line0 >= len(lines):
                continue
            refs.append(
                Reference(
                    path=ref_path,
                    line=line0 + 1,
                    column=_column(lines[line0], location.range.start.character, self._encoding),
                )
            )
        return refs
