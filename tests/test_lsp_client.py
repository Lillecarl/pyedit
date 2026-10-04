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
import time

import pytest
from lsprotocol import types
from pygls.io_ import run_async
from pygls.lsp.server import LanguageServer

import pyedit
from pyedit.lsp_client import (
    CodeActionResult,
    LspSession,
    _column_to_units,
    _units_to_column,
)
from pyedit.session import EditSession

_logger = logging.getLogger("pyedit-test-stub")

_pyright = shutil.which("pyright-langserver")
requires_pyright = pytest.mark.skipif(_pyright is None, reason="pyright is not on PATH")

_ruff = shutil.which("ruff")
requires_ruff = pytest.mark.skipif(_ruff is None, reason="ruff is not on PATH")


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
        # ranged answers win over answers: (method, uri, only, range)
        # lets a test script different replies per request range
        self.ranged: dict = {}
        # every codeAction request the stub saw, in order
        self.code_actions_seen: list = []
        self.documents: dict[str, str] = {}
        self.closed: list[str] = []
        self.settings = None
        self.resolved: list = []
        # hook(server, uri) the stub calls on didOpen, so a test can
        # push server-to-client notifications mid-run
        self.on_open = None

    async def run(self, to_server: asyncio.StreamReader, to_client) -> None:
        server = LanguageServer("stub", "0.1")

        def rename(params):
            key = ("rename", params.text_document.uri, params.position.line, params.position.character)
            return self.answers.get(key)

        def references(params):
            key = ("references", params.text_document.uri, params.position.line, params.position.character)
            return self.answers.get(key)

        def code_action(params):
            uri = params.text_document.uri
            only = tuple(params.context.only or ())
            extent = (
                params.range.start.line,
                params.range.start.character,
                params.range.end.line,
                params.range.end.character,
            )
            self.code_actions_seen.append((uri, only, extent))
            answer = self.ranged.get(("codeAction", uri, only, extent))
            if answer is None:
                answer = self.answers.get(("codeAction", uri, only))
            if isinstance(answer, Exception):
                raise answer
            return answer

        def resolve(params):
            self.resolved.append(params.title)
            answer = self.answers.get(("resolve", params.title))
            if isinstance(answer, Exception):
                raise answer
            return answer

        def did_open(params):
            self.documents[params.text_document.uri] = params.text_document.text
            if self.on_open is not None:
                self.on_open(server, params.text_document.uri)

        def did_change(params):
            self.documents[params.text_document.uri] = params.content_changes[-1].text

        def did_close(params):
            self.closed.append(params.text_document.uri)
            self.documents.pop(params.text_document.uri, None)

        def did_change_configuration(params):
            self.settings = params.settings

        server.feature(types.TEXT_DOCUMENT_RENAME)(rename)
        server.feature(types.TEXT_DOCUMENT_REFERENCES)(references)
        server.feature(types.TEXT_DOCUMENT_CODE_ACTION)(code_action)
        server.feature("codeAction/resolve")(resolve)
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
    assert _column_to_units(line, 1, "utf-16") == 2
    assert _column_to_units(line, 1, "utf-8") == 4
    assert _units_to_column(line, 2, "utf-16") == 1
    assert _units_to_column(line, 4, "utf-8") == 1
    assert _column_to_units(line, 3, "utf-32") == 3
    assert _units_to_column(line, 3, "utf-32") == 3


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


def _action_edit(uri, text="ALPHA"):
    return [
        types.CodeAction(
            title="Fix it",
            kind="source.fix",
            edit=types.WorkspaceEdit(
                changes={uri: [types.TextEdit(range=rng(0, 0, 0, 5), new_text=text)]}
            ),
        )
    ]


def test_code_action_stages_edits(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = _action_edit(uri_a)
    staged = handle.code_action("a.txt", "source.fix")
    assert staged == [session.root / "a.txt"]
    assert session.read("a.txt") == "ALPHA beta\n"


def test_code_action_kinds_pass_verbatim(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("codeAction", uri_a, ("a", "b"))] = []
    assert handle.code_action("a.txt", ["a", "b"]) == []
    with pytest.raises(ValueError, match="kind"):
        handle.code_action("a.txt", "")
    with pytest.raises(ValueError, match="kind"):
        handle.code_action("a.txt", [])
    with pytest.raises(ValueError, match="on_error"):
        handle.code_action_all("*.txt", "source.fix", on_error="loud")


def test_code_action_resolves_deferred_edits(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = [
        types.CodeAction(title="Fix it", kind="source.fix", data={"uri": uri_a})
    ]
    stub.answers[("resolve", "Fix it")] = _action_edit(uri_a)[0]
    assert handle.code_action("a.txt", "source.fix") == [session.root / "a.txt"]
    assert stub.resolved == ["Fix it"]
    assert session.read("a.txt") == "ALPHA beta\n"


def test_code_action_command_raises(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = [
        types.Command(title="Do it", command="server.doIt")
    ]
    with pytest.raises(ValueError, match="command"):
        handle.code_action("a.txt", "source.fix")
    assert session.read("a.txt") == "alpha beta\n"


def test_code_action_all_runs_scopes_and_reports(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = _action_edit(uri_a)
    report = handle.code_action_all("*.txt", "source.fix")
    assert isinstance(report, CodeActionResult)
    assert report.staged == [session.root / "a.txt"]
    assert report.skipped == {}
    assert session.read("a.txt") == "ALPHA beta\n"
    assert session.read("b.txt") == "gamma delta\n"


def test_code_action_all_skip_collects_errors(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    session.write("b.txt", "gamma delta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = _action_edit(uri_a)
    stub.answers[("codeAction", uri_b, ("source.fix",))] = RuntimeError("boom")
    report = handle.code_action_all("*.txt", "source.fix", on_error="skip")
    assert report.staged == [session.root / "a.txt"]
    assert list(report.skipped) == ["b.txt"]
    assert "boom" in report.skipped["b.txt"]
    assert session.read("b.txt") == "gamma delta\n"


def test_code_action_all_raise_keeps_earlier_files(lsp):
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    uri_b = (session.root / "b.txt").as_uri()
    session.write("a.txt", "alpha beta\n")
    session.write("b.txt", "gamma delta\n")
    stub.answers[("codeAction", uri_a, ("source.fix",))] = _action_edit(uri_a)
    stub.answers[("codeAction", uri_b, ("source.fix",))] = RuntimeError("boom")
    with pytest.raises(ValueError, match="boom"):
        handle.code_action_all("*.txt", "source.fix")
    assert session.read("a.txt") == "ALPHA beta\n"
    assert session.read("b.txt") == "gamma delta\n"


def test_code_action_all_discards_the_failing_scope(lsp):
    # no staged write here: the scope stages ALPHA, the second kind
    # raises, and the discard must leave the session empty
    handle, session, stub = lsp
    uri_a = (session.root / "a.txt").as_uri()
    stub.answers[("codeAction", uri_a, ("good",))] = _action_edit(uri_a)
    stub.answers[("codeAction", uri_a, ("bad",))] = RuntimeError("boom")
    with pytest.raises(ValueError, match="boom"):
        handle.code_action_all("a.txt", ["good", "bad"])
    # the scope's ALPHA must not leak; pruning drops the session's
    # materialized reads (the bridge didOpen'd the tree on connect),
    # so whatever remains past the prune is a real staged change
    assert session.read("a.txt") == "alpha beta\n"
    session.prune_unchanged()
    assert session.staged() == {}


def test_format_file_refuses_without_capability(lsp):
    handle, session, _stub = lsp
    session.write("a.txt", "alpha  beta\n")
    with pytest.raises(ValueError, match="formatting"):
        handle.format_file("a.txt")


def test_publish_diagnostics_are_tracked_without_warnings(stub_project, caplog):
    # issue #23: every push logged "Ignoring notification", hundreds
    # of lines per run into stderr and agent context captures
    stub = Stub()
    diag = types.Diagnostic(
        range=rng(0, 0, 0, 5),
        message="undefined",
        severity=types.DiagnosticSeverity.Error,
    )

    def on_open(server, uri):
        server.protocol.notify(
            types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS,
            types.PublishDiagnosticsParams(uri=uri, diagnostics=[diag]),
        )

    stub.on_open = on_open
    (stub_project.root / "a.py").write_text("x = 1\n")
    stub_project.write("a.py", "x = 1\n")
    with (
        caplog.at_level(logging.WARNING, logger="pygls.protocol.json_rpc"),
        LspSession(stub_project, [], in_memory=stub.run) as handle,
    ):
        uri = (stub_project.root / "a.py").as_uri()
        stub.answers[("references", uri, 0, 0)] = []
        handle.references("a.py", 1, 0, "x")
        for _ in range(200):
            if handle.diagnostics_for("a.py"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("the stub's diagnostics never arrived")
    assert [d.message for d in handle.diagnostics_for("a.py")] == ["undefined"]
    assert "Ignoring notification" not in caplog.text


def test_server_pushes_are_logged_not_warned(stub_project, caplog):
    # every other server→client push took the same "Ignoring
    # notification" path as diagnostics did (issue #23)
    stub = Stub()

    def on_open(server, uri):
        notify = server.protocol.notify
        notify(
            types.WINDOW_SHOW_MESSAGE,
            types.ShowMessageParams(
                type=types.MessageType.Error, message="boom-error"
            ),
        )
        notify(
            types.WINDOW_SHOW_MESSAGE,
            types.ShowMessageParams(
                type=types.MessageType.Info, message="just-info"
            ),
        )
        notify(
            types.WINDOW_LOG_MESSAGE,
            types.LogMessageParams(
                type=types.MessageType.Warning, message="careful"
            ),
        )
        notify(types.TELEMETRY_EVENT, {"anything": True})
        notify(
            types.PROGRESS,
            types.ProgressParams(token="t", value={"kind": "begin"}),
        )

    stub.on_open = on_open
    (stub_project.root / "a.py").write_text("x = 1\n")
    stub_project.write("a.py", "x = 1\n")
    # one level for the capture handler: nested at_level calls fight
    # over it, and the last one wins
    with (
        caplog.at_level(logging.DEBUG),
        LspSession(stub_project, [], in_memory=stub.run) as handle,
    ):
        uri = (stub_project.root / "a.py").as_uri()
        stub.answers[("references", uri, 0, 0)] = []
        handle.references("a.py", 1, 0, "x")
    assert "Ignoring notification" not in caplog.text
    # telemetry and progress are dropped without a sound: the only
    # server messages the bridge logs are the three routed ones
    # (run_async borrows the same logger for Content length debugs)
    assert {
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("language server:")
    } == {
        "language server: boom-error",
        "language server: just-info",
        "language server: careful",
    }
    by_message = {r.getMessage(): r.levelno for r in caplog.records}
    assert by_message.get("language server: boom-error") == logging.ERROR
    assert by_message.get("language server: careful") == logging.WARNING
    assert by_message.get("language server: just-info") == logging.INFO


def test_server_requests_get_headless_answers(stub_project):
    # a server may ask at any time (pyright asks workspace/
    # configuration); every answer is pinned against the stub so a
    # missing handler fails loudly instead of warning per message
    stub = Stub()
    target = (stub_project.root / "a.py").as_uri()
    answers: dict = {}

    def on_open(server, uri):
        if uri != target:
            return
        # on_open runs on the server's loop thread: schedule the
        # questions as a task there (a helper thread has no loop to
        # build the requests on)
        async def ask_all():
            calls = [
                (
                    types.WINDOW_SHOW_MESSAGE_REQUEST,
                    types.ShowMessageRequestParams(
                        type=types.MessageType.Error, message="pick?"
                    ),
                ),
                (
                    types.WINDOW_SHOW_DOCUMENT,
                    types.ShowDocumentParams(uri=target),
                ),
                (
                    types.WINDOW_WORK_DONE_PROGRESS_CREATE,
                    types.WorkDoneProgressCreateParams(token="t"),
                ),
                (
                    types.CLIENT_REGISTER_CAPABILITY,
                    types.RegistrationParams(
                        registrations=[
                            types.Registration(
                                id="r",
                                method="textDocument/didChangeWatchedFiles",
                            )
                        ]
                    ),
                ),
                (
                    types.CLIENT_UNREGISTER_CAPABILITY,
                    types.UnregistrationParams(
                        unregisterations=[
                            types.Unregistration(
                                id="r",
                                method="textDocument/didChangeWatchedFiles",
                            )
                        ]
                    ),
                ),
                (
                    types.WORKSPACE_APPLY_EDIT,
                    types.ApplyWorkspaceEditParams(
                        edit=types.WorkspaceEdit(changes={}), label="test"
                    ),
                ),
                (
                    types.WORKSPACE_CONFIGURATION,
                    types.ConfigurationParams(
                        items=[
                            types.ConfigurationItem(section="python"),
                            types.ConfigurationItem(section="other"),
                        ]
                    ),
                ),
                (types.WORKSPACE_WORKSPACE_FOLDERS, None),
            ]
            for method, params in calls:
                answers[method] = await server.protocol.send_request_async(
                    method, params
                )

        asyncio.ensure_future(ask_all())

    stub.on_open = on_open
    (stub_project.root / "a.py").write_text("x = 1\n")
    stub_project.write("a.py", "x = 1\n")
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        stub.answers[("references", target, 0, 0)] = []
        handle.references("a.py", 1, 0, "x")
        for _ in range(500):
            if len(answers) == 8:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the stub's requests were never answered")
    assert answers[types.WINDOW_SHOW_MESSAGE_REQUEST] is None
    assert answers[types.WINDOW_SHOW_DOCUMENT].success is False
    assert answers[types.WINDOW_WORK_DONE_PROGRESS_CREATE] is None
    assert answers[types.CLIENT_REGISTER_CAPABILITY] is None
    assert answers[types.CLIENT_UNREGISTER_CAPABILITY] is None
    assert answers[types.WORKSPACE_APPLY_EDIT].applied is False
    # JSON has no tuple: the stub structures the answer into one
    assert list(answers[types.WORKSPACE_CONFIGURATION]) == [None, None]
    assert [w.uri for w in answers[types.WORKSPACE_WORKSPACE_FOLDERS]] == [
        stub_project.root.as_uri()
    ]


def _push_unknown_name(stub):
    """Push one unknown-name diagnostic per didOpen; the pyrefly shape:
    file-wide requests see nothing, the diagnostic's own range fixes it."""
    diag = types.Diagnostic(
        range=rng(0, 6, 0, 10),
        message="unknown",
        severity=types.DiagnosticSeverity.Error,
    )

    def on_open(server, uri):
        server.protocol.notify(
            types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS,
            types.PublishDiagnosticsParams(uri=uri, diagnostics=[diag]),
        )

    stub.on_open = on_open


def test_quickfix_expands_to_diagnostic_ranges(stub_project):
    stub = Stub()
    body = 'print(json.dumps({"a": 1}))\n'
    (stub_project.root / "a.py").write_text(body)
    stub_project.write("a.py", body)
    _push_unknown_name(stub)
    uri = (stub_project.root / "a.py").as_uri()
    stub.answers[("codeAction", uri, ("quickfix",))] = []
    stub.ranged[("codeAction", uri, ("quickfix",), (0, 6, 0, 10))] = [
        types.CodeAction(
            title="Insert import",
            kind="quickfix",
            edit=types.WorkspaceEdit(
                changes={
                    uri: [
                        types.TextEdit(
                            range=rng(0, 0, 0, 0), new_text="import json\n\n"
                        )
                    ]
                }
            ),
        )
    ]
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        staged = handle.code_action("a.py", "quickfix")
    assert staged == [stub_project.root / "a.py"]
    assert stub_project.read("a.py") == "import json\n\n" + body


def test_quickfix_dedupes_identical_edits(stub_project):
    # the same fix comes back file-wide and per-range; staging it
    # twice must be a silent no-op, not a doubled insertion
    stub = Stub()
    body = "x = 1\n"
    (stub_project.root / "a.py").write_text(body)
    stub_project.write("a.py", body)
    _push_unknown_name(stub)
    uri = (stub_project.root / "a.py").as_uri()
    fix = [
        types.CodeAction(
            title="Fix",
            kind="quickfix",
            edit=types.WorkspaceEdit(
                changes={
                    uri: [
                        types.TextEdit(
                            range=rng(0, 0, 0, 0), new_text="# fixed\n"
                        )
                    ]
                }
            ),
        )
    ]
    stub.answers[("codeAction", uri, ("quickfix",))] = fix
    stub.ranged[("codeAction", uri, ("quickfix",), (0, 6, 0, 10))] = fix
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        handle.code_action("a.py", "quickfix")
    assert stub_project.read("a.py") == "# fixed\n" + body


def test_disabled_actions_are_skipped(stub_project):
    # a disabled action is unusable by construction; skipping comes
    # before resolve, so no round trip goes out for it
    stub = Stub()
    (stub_project.root / "a.py").write_text("x = 1\n")
    stub_project.write("a.py", "x = 1\n")
    uri = (stub_project.root / "a.py").as_uri()
    stub.answers[("codeAction", uri, ("quickfix",))] = [
        types.CodeAction(
            title="Nope",
            kind="quickfix",
            disabled=types.CodeActionDisabled(reason="not here"),
            edit=types.WorkspaceEdit(
                changes={
                    uri: [types.TextEdit(range=rng(0, 0, 0, 0), new_text="BAD\n")]
                }
            ),
        )
    ]
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        assert handle.code_action("a.py", "quickfix") == []
    assert stub_project.read("a.py") == "x = 1\n"
    assert stub.resolved == []


def test_only_titles_selects_among_alternatives(stub_project):
    # the pyrefly unknown-name shape: several alternatives for one
    # diagnostic, only the selected prefix stages
    stub = Stub()
    body = "x = 1\n"
    (stub_project.root / "a.py").write_text(body)
    stub_project.write("a.py", body)
    uri = (stub_project.root / "a.py").as_uri()
    stub.answers[("codeAction", uri, ("quickfix",))] = [
        types.CodeAction(
            title="Insert import: `json`",
            kind="quickfix",
            edit=types.WorkspaceEdit(
                changes={
                    uri: [
                        types.TextEdit(
                            range=rng(0, 0, 0, 0), new_text="import json\n"
                        )
                    ]
                }
            ),
        ),
        types.CodeAction(
            title="Generate variable `x`",
            kind="quickfix",
            edit=types.WorkspaceEdit(
                changes={
                    uri: [
                        types.TextEdit(
                            range=rng(1, 0, 1, 0), new_text="x = None\n"
                        )
                    ]
                }
            ),
        ),
    ]
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        staged = handle.code_action("a.py", "quickfix", only_titles=("Insert import",))
    assert staged == [stub_project.root / "a.py"]
    assert stub_project.read("a.py") == "import json\n" + body


def test_source_kinds_skip_diagnostic_expansion(stub_project):
    # source.* actions are file-wide by convention; diagnostics are
    # tracked but no per-range requests go out for them
    stub = Stub()
    (stub_project.root / "a.py").write_text("x = 1\n")
    stub_project.write("a.py", "x = 1\n")
    _push_unknown_name(stub)
    uri = (stub_project.root / "a.py").as_uri()
    stub.answers[("codeAction", uri, ("source.fixAll",))] = []
    with LspSession(stub_project, [], in_memory=stub.run) as handle:
        assert handle.code_action("a.py", "source.fixAll") == []
    assert [seen[1] for seen in stub.code_actions_seen] == [("source.fixAll",)]


def _ruff_project(tmp_path):
    (tmp_path / "fix.py").write_text("import os\nimport sys\n\nprint(sys.argv)\n")
    (tmp_path / "order.py").write_text("import sys\nimport os\n\nprint(os.name, sys.argv)\n")
    (tmp_path / "clean.py").write_text("import sys\n\nprint(sys.argv)\n")
    (tmp_path / "messy.py").write_text("x=1\n")
    return EditSession(respect_gitignore=False, root=tmp_path)


@requires_ruff
def test_ruff_fix_all_removes_unused_import(tmp_path):
    session = _ruff_project(tmp_path)
    with LspSession(session, ["ruff", "server"]) as handle:
        staged = handle.code_action("fix.py", "source.fixAll.ruff")
    assert staged == [tmp_path / "fix.py"]
    assert session.read("fix.py") == "import sys\n\nprint(sys.argv)\n"
    assert (tmp_path / "fix.py").read_text() == "import os\nimport sys\n\nprint(sys.argv)\n"


@requires_ruff
def test_ruff_organize_imports(tmp_path):
    session = _ruff_project(tmp_path)
    with LspSession(session, ["ruff", "server"]) as handle:
        staged = handle.code_action("order.py", "source.organizeImports.ruff")
    assert staged == [tmp_path / "order.py"]
    assert session.read("order.py") == "import os\nimport sys\n\nprint(os.name, sys.argv)\n"


@requires_ruff
def test_ruff_tree_fixes_only_dirty_files(tmp_path):
    session = _ruff_project(tmp_path)
    with LspSession(session, ["ruff", "server"]) as handle:
        report = handle.code_action_all(
            "*.py", ["source.fixAll.ruff", "source.organizeImports.ruff"]
        )
    assert {p.name for p in report.staged} == {"fix.py", "order.py"}
    assert report.skipped == {}
    assert session.read("fix.py") == "import sys\n\nprint(sys.argv)\n"
    assert session.read("clean.py") == "import sys\n\nprint(sys.argv)\n"


@requires_ruff
def test_ruff_format_file(tmp_path):
    session = _ruff_project(tmp_path)
    with LspSession(session, ["ruff", "server"]) as handle:
        assert handle.format_file("messy.py") == [tmp_path / "messy.py"]
        assert handle.format_file("clean.py") == []
    assert session.read("messy.py") == "x = 1\n"


@requires_ruff
def test_ruff_syntax_error_is_empty_not_an_error(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n")
    session = EditSession(respect_gitignore=False, root=tmp_path)
    with LspSession(session, ["ruff", "server"]) as handle:
        assert handle.code_action("broken.py", "source.fixAll.ruff") == []


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
