"""Tests for /alias and /sessions commands."""

from __future__ import annotations

import pytest

from antigona.cli_ui import aliases
from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.commands import CommandDisposition, CommandKind, parse_command
from antigona.cli_ui.models import ChatMessageRole


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGONA_CLI_ALIASES_FILE", str(tmp_path / "aliases.json"))
    monkeypatch.setenv("ANTIGONA_SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    yield


# ── aliases module ─────────────────────────────────────────────────────────

def test_alias_set_load_delete_expand() -> None:
    aliases.set_alias("s", "/status")
    aliases.set_alias("ls", "/list --all")
    assert aliases.load_aliases() == {"s": "/status", "ls": "/list --all"}

    assert aliases.expand("/s 123") == "/status 123"
    assert aliases.expand("ls") == "/list --all"
    assert aliases.expand("/model") == "/model"

    assert aliases.delete_alias("s") is True
    assert aliases.delete_alias("s") is False
    assert "s" not in aliases.load_aliases()


def test_alias_valid_name() -> None:
    assert aliases.valid_name("s")
    assert aliases.valid_name("my-cmd_2")
    assert not aliases.valid_name("has space")
    assert not aliases.valid_name("")
    assert not aliases.valid_name("with/ slash")
    assert not aliases.valid_name("a" * 30)


def test_alias_set_invalid_raises() -> None:
    with pytest.raises(ValueError):
        aliases.set_alias("bad name", "/status")


def test_alias_parse() -> None:
    assert parse_command("/alias").kind == CommandKind.ALIAS
    assert parse_command("/alias s /status").args == ("s", "/status")
    assert parse_command("/alias --del s").kind == CommandKind.ALIAS
    assert parse_command("/alias a b c").kind == CommandKind.ALIAS
    assert parse_command("/alias !!!x y").kind == CommandKind.MALFORMED


# ── sessions ───────────────────────────────────────────────────────────────

def test_sessions_parse() -> None:
    assert parse_command("/sessions").kind == CommandKind.SESSIONS
    assert parse_command("/sessions x").kind == CommandKind.MALFORMED


# ── controller dispatch (async) ───────────────────────────────────────────

def _controller() -> ChatController:
    return ChatController(None, renderer=None, poll_interval_sec=0.001,
                          max_poll_sec=5.0, conversation_id="cli-session",
                          enable_animations=False)


@pytest.mark.asyncio
async def test_handle_alias_list_empty() -> None:
    ctl = _controller()
    disp = await ctl.handle_input("/alias")
    assert disp == CommandDisposition.LOCAL_ACTION
    infos = [m.content for m in ctl.state.messages if m.role == ChatMessageRole.INFO]
    assert any("пока нет" in i for i in infos)


@pytest.mark.asyncio
async def test_handle_alias_set_then_expand(tmp_path) -> None:
    ctl = _controller()
    disp = await ctl.handle_input("/alias st /status")
    assert disp == CommandDisposition.LOCAL_ACTION
    # alias persisted
    assert aliases.load_aliases() == {"st": "/status"}

    # now /st should expand and dispatch as /status (via gateway-less local path
    # it lands in the dispatcher which requires a gateway -> we just verify
    # expansion changed the parsed kind to STATUS after alias expansion)
    from antigona.cli_ui.commands import parse_command
    assert parse_command(aliases.expand("/st 42")).kind == CommandKind.STATUS


@pytest.mark.asyncio
async def test_handle_alias_delete() -> None:
    aliases.set_alias("tmp", "/health")
    ctl = _controller()
    disp = await ctl.handle_input("/alias --del tmp")
    assert disp == CommandDisposition.LOCAL_ACTION
    assert "tmp" not in aliases.load_aliases()


@pytest.mark.asyncio
async def test_handle_sessions_lists_from_db() -> None:
    from antigona.core.paths import sessions_db_path
    from antigona.sessions.database import SessionDatabase

    async with SessionDatabase(str(sessions_db_path())) as db:
        await db.create_session("sess-abc", title="первый диалог", status="active")

    ctl = _controller()
    disp = await ctl.handle_input("/sessions")
    assert disp == CommandDisposition.LOCAL_ACTION
    infos = [m.content for m in ctl.state.messages if m.role == ChatMessageRole.INFO]
    assert any("sess-abc" in i for i in infos)


@pytest.mark.asyncio
async def test_handle_sessions_empty_db() -> None:
    ctl = _controller()
    disp = await ctl.handle_input("/sessions")
    assert disp == CommandDisposition.LOCAL_ACTION
    infos = [m.content for m in ctl.state.messages if m.role == ChatMessageRole.INFO]
    assert any("Сессий пока нет" in i for i in infos)
