"""Tests for the owner-gated tmux session tool and its dispatch plumbing.

Covers:
  * hard owner gate (configured/missing/mismatched owner, missing context);
  * quiet actions (start/send/read/list/status/kill) against real tmux when
    available (skipped otherwise);
  * blocked destructive commands;
  * registration via register_builtins();
  * DialogueEngine threading the authenticated owner into dispatch and never
    trusting a model-supplied ``_owner_id``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.tools import tmux_session
from antigona.tools.registry import ToolRegistry, register_builtins

OWNER = "424242"


@pytest.fixture(autouse=True)
def owner_env():
    with patch.dict(os.environ, {"ANTIGONA_OWNER_ID": OWNER}, clear=False):
        yield


# ── Owner gate (hard deny, fail-closed) ───────────────────────────────────────


def test_owner_gate_allows_owner():
    assert tmux_session._owner_gate(OWNER) is None


def test_owner_gate_denies_non_owner():
    err = tmux_session._owner_gate("999999")
    assert err is not None
    assert "только владельцу" in err
    assert OWNER in err


def test_owner_gate_denies_missing_context():
    err = tmux_session._owner_gate("")
    assert err is not None
    assert "контекст владельца отсутствует" in err


def test_owner_gate_fail_closed_without_config():
    with patch.dict(os.environ, {}, clear=True):
        err = tmux_session._owner_gate(OWNER)
        assert err is not None
        assert "не сконфигурирован" in err


def test_owner_gate_denies_invalid_owner_id():
    assert tmux_session._owner_gate("not-a-number") is not None


# ── Dispatch-level guard ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_handle_tmux_denies_non_owner():
    out = json.loads(await tmux_session._handle_tmux(action="list", _owner_id="someone-else"))
    assert "error" in out
    assert "только владельцу" in out["error"]


@pytest.mark.anyio
async def test_handle_tmux_denies_missing_owner_kwarg():
    out = json.loads(await tmux_session._handle_tmux(action="list"))
    assert "error" in out
    assert "контекст владельца" in out["error"]


@pytest.mark.anyio
async def test_handle_tmux_reports_missing_binary():
    with patch.object(tmux_session.shutil, "which", return_value=None):
        out = json.loads(await tmux_session._handle_tmux(action="list", _owner_id=OWNER))
    assert out == {"error": "tmux не установлен"}


@pytest.mark.anyio
async def test_handle_tmux_unknown_action():
    out = json.loads(await tmux_session._handle_tmux(action="explode", _owner_id=OWNER))
    assert "unknown action" in out["error"]


@pytest.mark.anyio
async def test_handle_tmux_blocks_destructive_command():
    out = json.loads(
        await tmux_session._handle_tmux(action="start", command="shutdown now", _owner_id=OWNER)
    )
    assert "заблокирована" in out["error"]


# ── Real tmux lifecycle (skipped when tmux is unavailable) ────────────────────


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason='tmux is not available on Windows (Wave 4)')
async def test_tmux_lifecycle_quiet():
    session = f"test-antigona-{uuid.uuid4().hex[:8]}"
    try:
        # start: detached session running a persistent command (prints then sleeps)
        started = json.loads(
            await tmux_session._handle_tmux(
                action="start", session=session, command="echo hi; sleep 30", _owner_id=OWNER
            )
        )
        assert started.get("success") is True
        assert started.get("session") == session

        # status: running
        status = json.loads(
            await tmux_session._handle_tmux(action="status", session=session, _owner_id=OWNER)
        )
        assert status.get("running") is True

        # read: output tail present
        read = json.loads(
            await tmux_session._handle_tmux(action="read", session=session, _owner_id=OWNER)
        )
        assert read.get("success") is True
        assert "hi" in read.get("lines", "")

        # list: our session is present
        listed = json.loads(await tmux_session._handle_tmux(action="list", _owner_id=OWNER))
        assert session in listed.get("sessions", [])

        # send: feed a key into the session (harmless in a finished shell)
        sent = json.loads(
            await tmux_session._handle_tmux(action="send", session=session, keys="true", _owner_id=OWNER)
        )
        assert sent.get("success") is True
    finally:
        # kill: cleanup
        killed = json.loads(
            await tmux_session._handle_tmux(action="kill", session=session, _owner_id=OWNER)
        )
        assert killed.get("success") is True
        status = json.loads(
            await tmux_session._handle_tmux(action="status", session=session, _owner_id=OWNER)
        )
        assert status.get("running") is False


def test_session_name_sanitized():
    assert tmux_session._sanitize_session("my:session/name!") == "my-session-name-"
    assert len(tmux_session._sanitize_session("x" * 100)) <= tmux_session._MAX_SESSION_LEN
    assert tmux_session._sanitize_session("  ")  # fallback name


# ── Registration ──────────────────────────────────────────────────────────────


def test_tmux_registered_in_builtins():
    registry = ToolRegistry()
    register_builtins(registry)
    tool = registry.get("tmux")
    assert tool is not None
    assert tool.name == "tmux"
    assert tool.toolset == "shell"
    assert tool.schema["properties"]["action"]["enum"] == list(tmux_session._ACTIONS)


# ── DialogueEngine owner threading (never trust the model payload) ────────────


@pytest.mark.anyio
async def test_maybe_run_tool_tmux_denied_without_grant_and_spoofed_owner_never_dispatched():
    """tmux via dialogue is grant-gated; a model-supplied ``_owner_id`` never dispatches."""
    registry = Mock()
    registry.dispatch = AsyncMock(return_value='{"success": true}')
    async with DialogueEngine(db_path=":memory:") as engine:
        engine.registry = registry

        reply = (
            '⟪tool:tmux action="list" _owner_id="999999"⟫'
        )
        out = await engine._maybe_run_tool(reply, owner_id=OWNER)

        # HIGH-risk tmux is denied at the unified policy boundary before any
        # dispatch, so the spoofed owner id never reaches the tool.
        registry.dispatch.assert_not_awaited()
        assert "999999" not in out
        assert "requires_approval" in out or "approval" in out.lower()


@pytest.mark.anyio
async def test_maybe_run_tool_does_not_inject_for_other_tools():
    registry = Mock()
    registry.dispatch = AsyncMock(return_value='{"success": true}')
    async with DialogueEngine(db_path=":memory:") as engine:
        engine.registry = registry

        await engine._maybe_run_tool('⟪tool:kanban action="list"⟫', owner_id=OWNER)

        _, kwargs = registry.dispatch.await_args
        assert "_owner_id" not in kwargs


@pytest.mark.anyio
async def test_reply_tmux_from_context_is_denied_without_approval_grant():
    """A model-emitted tmux call in the full reply path is grant-gated, not dispatched."""
    registry = Mock()
    registry.dispatch = AsyncMock(return_value='{"success": true}')
    async with DialogueEngine(db_path=":memory:") as engine:
        engine.registry = registry
        engine.provider = Mock()
        engine.provider.generate.return_value = '⟪tool:tmux action="list"⟫'

        out = await engine.reply(
            "список сессий", session_id="telegram:123", context={"owner_id": OWNER}
        )

        # HIGH-risk tmux never reaches dispatch without a real approval grant.
        registry.dispatch.assert_not_awaited()
        assert "approval" in out.lower() or "политик" in out.lower()


if __name__ == "__main__":
    pytest.main([__file__])
