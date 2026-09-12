"""The LSP client bridge, driven against a stub pygls server.

Real JSON-RPC both ways over in-memory duplex streams: the client runs
on its own background loop as it would with a subprocess, the stub
server answers initialize/rename/references through the same pygls
machinery a real server uses. No subprocesses anywhere.
"""

import asyncio
import logging
import shutil
import threading

import pytest
from lsprotocol import types
from pygls.io_ import run_async
from pygls.lsp.server import LanguageServer

import pyedit
from pyedit.lsp_client import LspSession, _column, _units
from pyedit.session import EditSession

_logger = logging.getLogger("pyedit-test-stub")

_pyright = shutil.which("pyright-langserver")
requires_pyright = pytest.mark.skipif(_pyright is None, reason="pyright is not on PATH")


def rng(line0, char0, line1, char1):
    return types.Range(
        start=types.Position(line=line0, character=char0),
        end=types.Position(line=line1, character=char1),
    )


class Stub:
    """Scripted answers plus a log of what the server was told.

    `answers` maps (method, uri, line, character) to the response the
    feature handler returns; missing keys answer null. `log` records
    document texts and close notifications so tests can assert the
    server saw the session's overlay.
    """

    def __init__(self) -> None:
        self.answers: dict = {}
        self.documents: dict[str, str] = {}
        self.closed: list[str] = []
        self.settings = None

    async def run(self, to_server: asyncio.StreamReader, to_client) -> None:
        server = LanguageServer("stub", "0.1")

        def rename(params):
            key = ("rename", params.text_document.uri, params.position.line, params.position.character)
            return self.answers.get(key)

        def references(params):
            key = ("references", params.text_document.uri, params.position.line, params.position.character)
            return self.answers.get(key)

        def did_open(params):
            self.documents[params.text_document.uri] = params.text_document.text

        def did_change(params):
            self.documents[params.text_document.uri] = params.content_changes[-1].text

        def did_close(params):
            self.closed.append(params.text_document.uri)
            self.documents.pop(params.text_document.uri, None)

        def did_change_configuration(params):
            self.settings = params.settings

        server.feature(types.TEXT_DOCUMENT_RENAME)(rename)
        server.feature(types.TEXT_DOCUMENT_REFERENCES)(references)
        server.feature(types.TEXT_DOCUMENT_DID_OPEN)(did_open)
        server.feature(types.TEXT_DOCUMENT_DID_CHANGE)(did_change)
        server.feature(types.TEXT_DOCUMENT_DID_CLOSE)(did_close)
        server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)(did_change_configuration)

        server.protocol.set_writer(to_client)
        await run_async(
            stop_event=threading.Event(),
            reader=to_server,
            protocol=server.protocol,
            logger=_logger,
        )


@pytest.fixture
def stub_project(tmp_path):
    (tmp_path / "a.txt").write_text("alpha beta\n")
    (tmp_path / "b.txt").write_text("gamma delta\n")
    return EditSession(respect_gitignore=False, root=tmp_path)


@pytest.fixture
def lsp(stub_project):
    stub = Stub()
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        yield handle, stub_project, stub


def test_python_path_reaches_the_server(stub_project):
    stub = Stub()
    with LspSession(stub_project, [], in_memory=stub.run, python_path="/opt/py"):
        pass
    assert stub.settings == {"python": {"pythonPath": "/opt/py"}}


def test_no_python_path_sends_no_configuration(stub_project):
    stub = Stub()
    with LspSession(stub_project, [], in_memory=stub.run):
        pass
    assert stub.settings is None


def test_rename_stages_server_edits_across_files(lsp):
    handle, session, stub = lsp
    # stage both files so the server sees the overlay, not disk
    session.write("a.txt", "alpha beta\n")
    session.write("b.txt", "staged gamma\n")
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    stub.answers[("rename", uri_a, 0, 0)] = types.WorkspaceEdit(
        changes={
            uri_a: [types.TextEdit(range=rng(0, 0, 0, 5), new_text="ALPHA")],
            uri_b: [types.TextEdit(range=rng(0, 0, 0, 6), new_text="GAMMA")],
        }
    )
    staged = handle.rename_symbol("a.txt", 1, 0, "alpha", "ALPHA")
    assert set(staged) == {session.root / "a.txt", session.root / "b.txt"}
    assert session.read("a.txt") == "ALPHA beta\n"
    assert session.read("b.txt") == "GAMMA gamma\n"
    assert stub.documents[uri_b] == "staged gamma\n"


def test_rename_pushes_updates_after_first_operation(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("references", uri_a, 0, 0)] = []
    handle.references("a.txt", 1, 0, "alpha")
    assert stub.documents[uri_a] == "alpha beta\n"
    session.write("a.txt", "alpha rewrote\n")
    handle.references("a.txt", 1, 0, "alpha")
    assert stub.documents[uri_a] == "alpha rewrote\n"


def test_rename_without_server_answer_fails_loudly(lsp):
    handle, session, _stub = lsp
    with pytest.raises(ValueError, match="cannot rename"):
        handle.rename_symbol("a.txt", 1, 0, "alpha", "ALPHA")


def test_wrong_old_name_fails(lsp):
    handle, session, _stub = lsp
    with pytest.raises(ValueError, match="is 'beta', not 'wrong'"):
        handle.rename_symbol("a.txt", 1, 6, "wrong", "BETA")


def test_references_map_back_to_pyedit_positions(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("references", uri_a, 0, 0)] = [
        types.Location(uri=uri_a, range=rng(0, 0, 0, 5)),
        types.Location(uri=uri_b, range=rng(0, 6, 0, 11)),
    ]
    refs = handle.references("a.txt", 1, 0, "alpha")
    assert [(r.path.name, r.line, r.column) for r in refs] == [
        ("a.txt", 1, 0),
        ("b.txt", 1, 6),
    ]


def test_resource_operations_are_rejected(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("rename", uri_a, 0, 0)] = types.WorkspaceEdit(
        document_changes=[
            types.DeleteFile(kind="delete", uri=uri_a, options=None),
        ]
    )
    with pytest.raises(ValueError, match="resource operation"):
        handle.rename_symbol("a.txt", 1, 0, "alpha", "ALPHA")


def test_document_changes_shape_is_staged(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("rename", uri_a, 0, 0)] = types.WorkspaceEdit(
        document_changes=[
            types.TextDocumentEdit(
                text_document=types.VersionedTextDocumentIdentifier(uri=uri_a, version=1),
                edits=[types.TextEdit(range=rng(0, 0, 0, 5), new_text="ALPHA")],
            )
        ]
    )
    assert handle.rename_symbol("a.txt", 1, 0, "alpha", "ALPHA") == [session.root / "a.txt"]
    assert session.read("a.txt") == "ALPHA beta\n"


def test_close_notification_on_delete(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    session.write("b.txt", "gamma delta\n")
    stub.answers[("references", uri_a, 0, 0)] = []
    handle.references("a.txt", 1, 0, "alpha")
    assert stub.documents[uri_b] == "gamma delta\n"
    session.delete("b.txt")
    handle.references("a.txt", 1, 0, "alpha")
    assert uri_b in stub.closed
    assert uri_b not in stub.documents


def test_utf16_columns_convert_round_trip():
    # astral chars take two utf-16 units; BMP and ascii take one
    line = "𝐀xα y"
    assert _units(line, 1, "utf-16") == 2
    assert _units(line, 1, "utf-8") == 4
    assert _column(line, 2, "utf-16") == 1
    assert _column(line, 4, "utf-8") == 1
    assert _units(line, 3, "utf-32") == 3
    assert _column(line, 3, "utf-32") == 3


def test_overlapping_server_edits_are_rejected(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("rename", uri_a, 0, 0)] = types.WorkspaceEdit(
        changes={
            uri_a: [
                types.TextEdit(range=rng(0, 0, 0, 5), new_text="one"),
                types.TextEdit(range=rng(0, 2, 0, 3), new_text="two"),
            ]
        }
    )
    with pytest.raises(ValueError, match="overlapping"):
        handle.rename_symbol("a.txt", 1, 0, "alpha", "ALPHA")


def test_lsp_binds_the_live_session(stub_project, monkeypatch):
    opened = []
    pyedit.session = stub_project
    try:
        monkeypatch.setattr(
            pyedit,
            "LspSession",
            lambda session, command, **kwargs: opened.append((session, command))
        )
        pyedit.lsp(["rust-analyzer"])
        assert opened == [(stub_project, ["rust-analyzer"])]
    finally:
        del pyedit.session
    with pytest.raises(AttributeError, match="no edit session"):
        pyedit.lsp(["rust-analyzer"])


def _pyright_project(tmp_path):
    (tmp_path / "alpha.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "beta.py").write_text("import alpha\n\nvalue = alpha.alpha()\n")
    return EditSession(respect_gitignore=False, root=tmp_path)


@requires_pyright
def test_pyright_rename_stages_across_files(tmp_path):
    session = _pyright_project(tmp_path)
    with LspSession(session, [_pyright, "--stdio"]) as handle:
        staged = handle.rename_symbol("alpha.py", 1, 5, "alpha", "omega")
    assert set(staged) == {tmp_path / "alpha.py", tmp_path / "beta.py"}
    assert session.read("alpha.py") == "def omega():\n    return 1\n"
    assert session.read("beta.py") == "import alpha\n\nvalue = alpha.omega()\n"


@requires_pyright
def test_pyright_references_find_all_occurrences(tmp_path):
    session = _pyright_project(tmp_path)
    with LspSession(session, [_pyright, "--stdio"]) as handle:
        refs = handle.references("alpha.py", 1, 5, "alpha")
    found = {(r.path.name, r.line, r.column) for r in refs}
    assert found == {
        ("alpha.py", 1, 4),
        ("beta.py", 3, 14),
    }


@requires_pyright
def test_pyright_sees_staged_content_not_disk(tmp_path):
    # disk holds alpha; the session has renamed it to gamma already --
    # the server must compute over the didOpen'd overlay
    (tmp_path / "alpha.py").write_text("def alpha():\n    return 1\n")
    session = EditSession(respect_gitignore=False, root=tmp_path)
    session.write("alpha.py", "def gamma():\n    return 2\n")
    with LspSession(session, [_pyright, "--stdio"]) as handle:
        refs = handle.references("alpha.py", 1, 5, "gamma")
    assert [(r.path, r.line, r.column) for r in refs] == [
        (tmp_path / "alpha.py", 1, 4)
    ]
