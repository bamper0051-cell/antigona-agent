"""Tests for the MCP-in-task flow: safety gate, brain routing, task plumbing, orchestrator."""

from __future__ import annotations

import pytest

from antigona.core.brain import (
    _known_install_reply,
    _parse_email_request,
    _parse_mcp_request,
)
from antigona.database import Database
from antigona.repository import CreateTask, SensitiveTaskInput, TaskRepository
from antigona.router.intent_router import IntentRouter
from antigona.schemas import TaskCreate

# ── Intent router: TTS / MCP / email requests classify as tasks ─────────────


def test_router_classifies_tts_mcp_request_as_task():
    router = IntentRouter()
    decision = router.route('Озвучь через mcp: server="edge-tts", tool="speak"')
    assert decision.intent == "task.mcp"


def test_router_classifies_explicit_mcp_call_format():
    router = IntentRouter()
    decision = router.route("вызови mcp server=edge-tts tool=list_available_voices")
    assert decision.intent == "task.mcp"


def test_router_classifies_short_email_request_as_task():
    # "отправь на почту" is 3 words — must NOT fall into the conversation shortcut
    router = IntentRouter()
    decision = router.route("Отправь на почту")
    assert decision.intent == "task.email"


def test_router_classifies_email_request_as_task():
    router = IntentRouter()
    decision = router.route("Отправь рассказ о себе на почту")
    assert decision.intent == "task.email"


def test_router_bare_mcp_mention_stays_conversation():
    router = IntentRouter()
    decision = router.route("расскажи, что умеет mcp")
    assert decision.intent.startswith(("conversation.", "question.", "analysis."))


def test_router_normal_task_unaffected():
    router = IntentRouter()
    decision = router.route("создай файл test.txt")
    assert decision.intent == "task.file_write"


# ── Brain routing: TTS defaults + email params ──────────────────────────────


def test_parse_mcp_request_tts_defaults():
    # A human TTS request resolves to the SINGLE working contract tool; there
    # is no edge-tts MCP server to advertise or call (no phantom capability).
    parsed = _parse_mcp_request("Озвучь рассказ о себе")
    assert parsed == {
        "kind": "tts",
        "server": "",
        "tool": "speech.tts",
        "arguments": {"text": "рассказ о себе"},
    }


def test_parse_mcp_request_tts_with_explicit_tool():
    # An explicit server=/tool= pair is an MCP request (validated for
    # registration before a flow is created).
    parsed = _parse_mcp_request('Озвучь: server="edge-tts", tool="list_available_voices"')
    assert parsed == {
        "kind": "mcp",
        "server": "edge-tts",
        "tool": "list_available_voices",
        "arguments": {},
    }


def test_parse_mcp_request_tts_email_combo(monkeypatch):
    """«Озвучь X и отправь на почту» → speak_to_email in one task."""
    monkeypatch.setenv("ANTIGONA_DELIVERY_EMAIL_TO", "user@example.com")
    parsed = _parse_mcp_request("Озвучь рассказ о себе и отправь на почту")
    assert parsed == {
        "kind": "tts",
        "server": "",
        "tool": "speech.tts",
        "arguments": {"text": "рассказ о себе", "to": "user@example.com"},
    }


def test_parse_mcp_request_tts_email_custom_address():
    parsed = _parse_mcp_request("Озвучь привет и отправь на test@example.com")
    assert parsed["kind"] == "tts"
    assert parsed["tool"] == "speech.tts"
    assert parsed["arguments"]["text"] == "привет"
    assert parsed["arguments"]["to"] == "test@example.com"


def test_parse_email_request_basic(monkeypatch):
    monkeypatch.setenv("ANTIGONA_DELIVERY_EMAIL_TO", "user@example.com")
    parsed = _parse_email_request("Отправь рассказ на почту")
    assert parsed is not None
    assert parsed["to"] == "user@example.com"
    assert parsed["attachment"] == ""


def test_parse_email_request_with_attachment():
    parsed = _parse_email_request("Отправь файл привет.mp3 на почту")
    assert parsed is not None
    assert parsed["attachment"] == "привет.mp3"


def test_parse_email_request_none_for_other_requests():
    assert _parse_email_request("создай файл test.txt") is None
    assert _parse_email_request("привет") is None


# ── Known-install replies (Install edge-tts → no sandbox task) ──────────────


def test_known_install_edge_tts_reply():
    reply = _known_install_reply(("Install edge-tts",), "Install edge-tts")
    assert reply is not None
    assert "уже установлен" in reply
    assert "edge-tts" in reply


def test_known_install_pip_uv_reply():
    reply = _known_install_reply(("pip install uv",), "pip install uv")
    assert reply is not None
    assert "uv" in reply


def test_known_install_unknown_package_returns_none():
    assert _known_install_reply(("pip install some-unknown-pkg-xyz",), "install xyz") is None
    assert _known_install_reply(("ls -la",), "ls -la") is None
    assert _known_install_reply((), "просто текст") is None


# ── Safety gate: HTML escaping must not look like a sensitive change ─────────


def test_sanitization_gate_quotes_not_sensitive():
    from antigona.repository import _sanitization_changes

    assert _sanitization_changes('озвучь text="привет" tool="speak"') is False
    assert _sanitization_changes('arguments={"text": "привет"}') is False
    assert _sanitization_changes("обычный текст без кавычек") is False


def test_sanitization_gate_secrets_still_sensitive():
    from antigona.repository import _sanitization_changes

    assert _sanitization_changes("password=super_secret_123") is True
    assert _sanitization_changes("Bearer abc.def.ghi") is True


def test_sanitization_gate_newlines_not_sensitive():
    # Telegram messages can contain line breaks — a newline is not a secret
    from antigona.repository import _sanitization_changes

    assert _sanitization_changes('"Напиши рассказ о себе и\nозвучь" / "… и отправь на почту"') is False
    assert _sanitization_changes("многострочный\nтекст") is False


def test_create_task_with_quotes_passes_gate(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner",
                goal='озвучь text="привет"',
                path="mcp-result",
                content='озвучь text="привет"',
                idempotency_key="k-quotes",
                tool_name="mcp",
                mcp_server="edge-tts",
                mcp_tool="speak",
                mcp_arguments={"text": "привет"},
            )
        )
        assert created is True
        assert task.tool_name == "mcp"


def test_create_task_secret_still_rejected(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        with pytest.raises(SensitiveTaskInput):
            repo.create(
                CreateTask(
                    owner_id="owner",
                    goal="передай password=hunter2",
                    path="x.txt",
                    content="передай password=hunter2",
                    idempotency_key="k-secret",
                )
            )


# ── Brain routing: _parse_mcp_request ───────────────────────────────────────


def test_parse_mcp_request_full():
    parsed = _parse_mcp_request(
        'Озвучь через mcp: server="edge-tts", tool="speak", '
        'arguments={"text": "привет", "voice": "ru-RU-SvetlanaNeural"}'
    )
    assert parsed == {
        "kind": "mcp",
        "server": "edge-tts",
        "tool": "speak",
        "arguments": {"text": "привет", "voice": "ru-RU-SvetlanaNeural"},
    }


def test_parse_mcp_request_no_mcp_returns_none():
    assert _parse_mcp_request("создай файл test.txt") is None
    assert _parse_mcp_request("привет") is None
    assert _parse_mcp_request("") is None


def test_parse_mcp_request_missing_server_or_tool_returns_none():
    # No phantom default server/tool is invented any more: an incomplete MCP
    # request is NOT an executable call.
    assert _parse_mcp_request("вызови mcp tool=speak") is None
    assert _parse_mcp_request("вызови mcp server=edge-tts") is None
    assert _parse_mcp_request("вызови mcp server=context7") is None


def test_parse_mcp_request_text_fallback():
    parsed = _parse_mcp_request(
        'mcp server="edge-tts" tool="speak" text="Привет, Антигона!"'
    )
    assert parsed["server"] == "edge-tts"
    assert parsed["tool"] == "speak"
    assert parsed["arguments"] == {"text": "Привет, Антигона!"}


# ── MCP task plumbing: CreateTask -> persisted tool_arguments ───────────────


def test_create_mcp_task_persists_tool_arguments(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner",
                goal="озвучь через mcp",
                path="mcp-result",
                content="озвучь через mcp",
                idempotency_key="k-mcp",
                tool_name="mcp",
                mcp_server="edge-tts",
                mcp_tool="speak",
                mcp_arguments={"text": "привет", "voice": "ru-RU-SvetlanaNeural"},
            )
        )
        assert created is True
        assert task.tool_name == "mcp"
        assert task.tool_arguments["server"] == "edge-tts"
        assert task.tool_arguments["tool"] == "speak"
        assert task.tool_arguments["arguments"] == {
            "text": "привет",
            "voice": "ru-RU-SvetlanaNeural",
        }
        assert "arguments_sha256" in task.tool_arguments
        # fingerprint distinguishes different mcp targets
        other = repo.create(
            CreateTask(
                owner_id="owner",
                goal="список голосов",
                path="mcp-result",
                content="список голосов",
                idempotency_key="k-mcp2",
                tool_name="mcp",
                mcp_server="edge-tts",
                mcp_tool="list_available_voices",
                mcp_arguments={"language": "ru"},
            )
        )[0]
        assert other.tool_arguments["tool"] == "list_available_voices"


def test_task_create_schema_accepts_mcp():
    model = TaskCreate(
        goal="озвучь",
        path="mcp-result",
        content="озвучь",
        tool_name="mcp",
        mcp_server="edge-tts",
        mcp_tool="speak",
        mcp_arguments={"text": "привет"},
    )
    assert model.tool_name == "mcp"
    assert model.mcp_server == "edge-tts"
    assert model.mcp_tool == "speak"
    assert model.mcp_arguments == {"text": "привет"}


# ── Orchestrator: mcp execution branch ──────────────────────────────────────


def monkeypatch_tmp_registry():
    """A fresh, isolated mcp_servers.json path (seeds only context7)."""
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp())
    return directory / "mcp_servers.json"


class _FakeTask:
    def __init__(self, tool_arguments: dict):
        self.tool_arguments = tool_arguments


def test_execute_mcp_tool_success(monkeypatch):
    from antigona.orchestrator import McpCallOutcome, Orchestrator

    monkeypatch.setattr(
        "antigona.orchestrator._run_async_mcp_call",
        lambda server, tool, arguments: McpCallOutcome(ok=True, value="Spoken: привет"),
    )
    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask(
        {
            "server": "edge-tts",
            "tool": "speak",
            "arguments": {"text": "привет"},
        }
    )
    result = orch._execute_mcp_tool(task)
    assert result.ok is True
    assert result.data["output"] == "Spoken: привет"


def test_execute_mcp_tool_missing_params_fails(monkeypatch):
    from antigona.orchestrator import Orchestrator

    def boom(*args, **kwargs):
        raise AssertionError("must not connect without server/tool")

    monkeypatch.setattr("antigona.orchestrator._run_async_mcp_call", boom)
    orch = Orchestrator.__new__(Orchestrator)
    result = orch._execute_mcp_tool(_FakeTask({"server": "", "tool": "speak", "arguments": {}}))
    assert result.ok is False
    assert "server/tool" in (result.error or "")


def test_execute_mcp_tool_call_failure(monkeypatch):
    from antigona.orchestrator import McpCallOutcome, Orchestrator

    monkeypatch.setattr(
        "antigona.orchestrator._run_async_mcp_call",
        lambda server, tool, arguments: McpCallOutcome(
            ok=False, reason="bridge_error", detail="tool 'speak' raised TimeoutError"
        ),
    )
    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak", "arguments": {"text": "x"}})
    result = orch._execute_mcp_tool(task)
    assert result.ok is False
    # A concrete, specific reason — never the old generic laundering string.
    assert result.error is not None
    assert "bridge_error" not in result.error
    assert "raise" in result.error or "edge-tts" in result.error


def test_execute_mcp_tool_unregistered_server_specific_error(monkeypatch):
    from antigona.core import mcp as mcp_module
    from antigona.orchestrator import Orchestrator

    # Real bridge: a fresh registry seeds only context7, so edge-tts is absent.
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", monkeypatch_tmp_registry())
    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak", "arguments": {"text": "x"}})
    result = orch._execute_mcp_tool(task)
    assert result.ok is False
    assert result.error is not None
    assert "edge-tts" in result.error
    assert "not registered" in result.error


def test_execute_mcp_tool_timeout_specific_error(monkeypatch):
    from antigona.orchestrator import McpCallOutcome, Orchestrator

    monkeypatch.setattr(
        "antigona.orchestrator._run_async_mcp_call",
        lambda server, tool, arguments: McpCallOutcome(
            ok=False, reason="timeout", detail="call 'speak' did not return within 120s"
        ),
    )
    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak", "arguments": {"text": "x"}})
    result = orch._execute_mcp_tool(task)
    assert result.ok is False
    assert "timed out" in (result.error or "")


def test_execute_mcp_tool_detects_file_result(monkeypatch):
    """speak_to_file returns {"file": ..., "spoken": ...} — the path must be carried."""
    import json as _json

    from antigona.orchestrator import McpCallOutcome, Orchestrator

    payload = _json.dumps({"file": "/tmp/x.mp3", "spoken": "привет"})
    monkeypatch.setattr(
        "antigona.orchestrator._run_async_mcp_call",
        lambda server, tool, arguments: McpCallOutcome(ok=True, value=payload),
    )
    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak_to_file", "arguments": {"text": "x"}})
    result = orch._execute_mcp_tool(task)
    assert result.ok is True
    assert result.data["file"] == "/tmp/x.mp3"


def test_materialize_mcp_file_artifact_copies_mp3(tmp_path):
    import hashlib

    from antigona.contracts import ToolResult
    from antigona.orchestrator import Orchestrator

    workspace = tmp_path / "ws"
    src = workspace / "mcp_output" / "tts.mp3"
    src.parent.mkdir(parents=True)
    payload = b"\xff\xfb\x90\x00" + b"audio" * 100
    src.write_bytes(payload)

    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak_to_file"})
    task.id = "flow-123"
    result = orch._materialize_mcp_file_artifact(
        task,
        ToolResult(True, "completed", data={"output": "x", "file": str(src)}),
        workspace,
    )

    assert result.ok is True
    assert len(result.artifacts) == 1
    art = result.artifacts[0]
    assert art.path == ".antigona-results/flow-123.mp3"
    destination = workspace / art.path
    assert destination.exists()
    assert destination.read_bytes() == payload
    assert art.sha256 == hashlib.sha256(payload).hexdigest()
    assert art.size == len(payload)


def test_materialize_mcp_file_artifact_rejects_outside_workspace(tmp_path):
    from antigona.contracts import ToolResult
    from antigona.orchestrator import Orchestrator

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x" * 10)

    orch = Orchestrator.__new__(Orchestrator)
    task = _FakeTask({"server": "edge-tts", "tool": "speak_to_file"})
    task.id = "flow-out"
    result = orch._materialize_mcp_file_artifact(
        task,
        ToolResult(True, "completed", data={"output": "x", "file": str(outside)}),
        workspace,
    )
    assert result.ok is False
    assert "outside workspace" in (result.error or "")


# ── send_email task branch ──────────────────────────────────────────────────


def test_create_send_email_task_persists_params(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner",
                goal="отправь на почту",
                path="email",
                content="отправь на почту",
                idempotency_key="k-email",
                tool_name="send_email",
                params={
                    "to": "user@example.com",
                    "subject": "Antigona delivery",
                    "body": "привет",
                    "attachment": ".antigona-results/abc.mp3",
                },
            )
        )
        assert created is True
        assert task.tool_name == "send_email"
        assert task.tool_arguments["to"] == "user@example.com"
        assert task.tool_arguments["attachment"] == ".antigona-results/abc.mp3"
        assert "arguments_sha256" in task.tool_arguments


def test_execute_send_email_success(monkeypatch):
    from antigona.orchestrator import Orchestrator

    calls: dict = {}

    def fake_send_email(to, subject, body, attachments):
        calls["to"] = to
        calls["subject"] = subject
        calls["body"] = body
        calls["attachments"] = list(attachments)
        return "Email sent to x (attachments: 1)"

    monkeypatch.setattr("antigona.core.email_sender.send_email", fake_send_email)
    orch = Orchestrator.__new__(Orchestrator)
    orch._execution_workspace_root = lambda task: None
    task = _FakeTask({
        "to": "user@example.com",
        "subject": "Antigona delivery",
        "body": "привет",
        "attachment": "audio.mp3",
    })
    task.id = "flow-email"
    task.goal = "отправь на почту"
    result = orch._execute_send_email(task)
    assert result.ok is True
    assert calls["to"] == "user@example.com"
    assert calls["attachments"] == ["audio.mp3"]
    assert "Email sent" in result.data["output"]


def test_execute_send_email_failure(monkeypatch):
    from antigona.orchestrator import Orchestrator

    def boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("antigona.core.email_sender.send_email", boom)
    orch = Orchestrator.__new__(Orchestrator)
    orch._execution_workspace_root = lambda task: None
    task = _FakeTask({"to": "x@y.z", "subject": "S", "body": "B"})
    task.id = "flow-email-fail"
    task.goal = "отправь на почту"
    result = orch._execute_send_email(task)
    assert result.ok is False
    assert "send_email execution failed" in (result.error or "")


def test_run_async_mcp_call_unregistered_server(tmp_path, monkeypatch):
    """Unregistered server must fail cleanly through the real bridge."""
    import antigona.orchestrator as orch_module
    from antigona.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    # fresh path -> registry seeds only context7; edge-tts is absent
    out = orch_module._run_async_mcp_call("edge-tts", "speak", {"text": "x"})
    assert out.ok is False
    assert out.reason == "unregistered_server"
    assert out.value is None
    # the mapped user-facing message is concrete and names the server
    message = orch_module._mcp_failure_message("edge-tts", "speak", out)
    assert "edge-tts" in message and "not registered" in message
