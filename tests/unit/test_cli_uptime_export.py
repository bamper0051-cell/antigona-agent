"""Tests for /uptime and /export commands (pure-local, no Gateway/LLM).

/uptime — read-only host system info from /proc.
/export — writes the current transcript to a markdown file bounded to ~.
Both are classified by the strict fail-closed parser and dispatched locally
in ChatController.handle_input without touching the Gateway.
"""

from __future__ import annotations

import os

import pytest

from antigona.cli_ui.chat import ChatController, _export_transcript, _system_info_text
from antigona.cli_ui.commands import CommandDisposition, CommandKind, parse_command
from antigona.cli_ui.models import ChatMessage, ChatMessageRole, ChatUIState
from antigona.core.paths import owner_dir

# ── parser classification (fail-closed) ───────────────────────────────────

def test_uptime_parse() -> None:
    assert parse_command("/uptime").kind == CommandKind.UPTIME
    assert parse_command("/sys").kind == CommandKind.UPTIME
    assert parse_command("/uptime").args == ()
    # extra args fail closed
    assert parse_command("/uptime x").kind == CommandKind.MALFORMED


def test_export_parse() -> None:
    assert parse_command("/export").kind == CommandKind.EXPORT
    assert parse_command("/export out.md").args == ("out.md",)
    # too many args fail closed
    assert parse_command("/export a b").kind == CommandKind.MALFORMED


# ── /uptime helper ────────────────────────────────────────────────────────

def test_system_info_text() -> None:
    text = _system_info_text()
    assert "Система" in text
    # at least uptime or cpu present (depends on /proc availability)
    assert any(k in text for k in ("Аптайм", "CPU", "Хост"))


# ── /export helper ────────────────────────────────────────────────────────

def _state(messages=None) -> ChatUIState:
    return ChatUIState(
        messages=messages or [
            ChatMessage(role=ChatMessageRole.USER, content="привет"),
            ChatMessage(role=ChatMessageRole.ASSISTANT, content="здравствуй"),
        ],
        current_status="idle",
        events=[],
        connection="connected",
        gateway_url="http://127.0.0.1:8090",
        session_id="test-session",
    )


def test_export_writes_relative_path() -> None:
    import shutil
    import tempfile

    st = _state()
    home = str(owner_dir())
    tmpdir = tempfile.mkdtemp(dir=home, prefix="export_test_")
    try:
        rel = os.path.relpath(tmpdir, home)
        out = _export_transcript(st, os.path.join(rel, "chat.md"))
        assert os.path.isfile(out)
        content = open(out, encoding="utf-8").read()
        assert "привет" in content
        assert "здравствуй" in content
        assert "test-session" in content
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_default_path_under_home() -> None:
    st = _state()
    out = _export_transcript(st, "")
    assert out.startswith(str(owner_dir()))
    assert os.path.isfile(out)
    os.remove(out)


def test_export_rejects_absolute_path(tmp_path) -> None:
    st = _state()
    with pytest.raises(ValueError):
        _export_transcript(st, "/etc/evil.md")


def test_export_rejects_traversal() -> None:
    st = _state()
    with pytest.raises(ValueError):
        _export_transcript(st, "../evil.md")


# ── handle_input dispatch (local, async) ─────────────────────────────────

def _controller() -> ChatController:
    return ChatController(
        None,  # gateway not needed for local commands
        renderer=None,
        poll_interval_sec=0.001,
        max_poll_sec=5.0,
        conversation_id="cli-session",
        enable_animations=False,
    )


@pytest.mark.asyncio
async def test_handle_uptime_dispatches_local() -> None:
    ctl = _controller()
    disp = await ctl.handle_input("/uptime")
    assert disp == CommandDisposition.LOCAL_ACTION
    assert any("Система" in m.content for m in ctl.state.messages if m.role == ChatMessageRole.INFO)


@pytest.mark.asyncio
async def test_handle_export_dispatches_local() -> None:
    import shutil
    import tempfile

    ctl = _controller()
    ctl.state.messages.append(ChatMessage(role=ChatMessageRole.USER, content="сохрани меня"))
    home = str(owner_dir())
    tmpdir = tempfile.mkdtemp(dir=home, prefix="export_test_")
    try:
        rel = os.path.relpath(tmpdir, home)
        disp = await ctl.handle_input(f"/export {os.path.join(rel, 'export.md')}")
        assert disp == CommandDisposition.LOCAL_ACTION
        target = os.path.join(home, rel, "export.md")
        assert os.path.isfile(target)
        assert "сохрани меня" in open(target, encoding="utf-8").read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
