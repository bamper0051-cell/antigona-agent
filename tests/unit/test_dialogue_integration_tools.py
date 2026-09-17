"""Tests: integration tools advertised + executed via DialogueEngine."""

from __future__ import annotations

import pytest

from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.tools.registry import ToolRegistry, register_builtins


@pytest.fixture
async def engine():
    reg = ToolRegistry()
    register_builtins(reg)
    async with DialogueEngine(registry=reg) as e:
        yield e


def test_tools_block_lists_integrations(engine):
    block = engine._integration_tools_block()
    for name in ("kanban", "mcp", "acp", "rss", "loop", "count_tokens"):
        assert name in block


@pytest.mark.asyncio
async def test_tools_block_empty_without_registry():
    async with DialogueEngine(registry=None) as e:
        assert e._integration_tools_block() == ""


@pytest.mark.asyncio
async def test_maybe_run_tool_executes_kanban(engine, tmp_path):
    call = chr(0x27ea) + (
        f'tool:kanban action="create" title="Т" board="{tmp_path}/kb"'
    ) + chr(0x27eb)
    result = await engine._maybe_run_tool(call)
    assert "Инструмент `kanban`" in result
    assert '"success": true' in result


@pytest.mark.asyncio
async def test_maybe_run_tool_passthrough_without_marker(engine):
    r = await engine._maybe_run_tool("просто текст")
    assert r == "просто текст"


@pytest.mark.asyncio
async def test_dispatch_pops_reserved_turn_id_context_params() -> None:
    """F-03.5 hygiene: reserved _turn_id/_correlation_id/_channel/_user_id/
    _session_id/_approval_token must be extracted in registry.dispatch and never
    leak into the tool handler kwargs (turn_id was previously passed through).
    """
    reg = ToolRegistry()
    received: dict[str, object] = {}

    async def handler(**kwargs: object) -> str:
        received.update(kwargs)
        return '{"ok": true}'

    reg.register("probe_reserved_tool", handler=handler)
    await reg.dispatch(
        "probe_reserved_tool",
        command="echo hi",
        _turn_id="turn-123",
        _correlation_id="corr-1",
        _channel="cli",
        _user_id="u1",
        _session_id="s1",
        _approval_token="tok",
    )

    for rk in ("_turn_id", "_correlation_id", "_channel", "_user_id", "_session_id", "_approval_token"):
        assert rk not in received, f"{rk} leaked into tool handler kwargs"
    assert received.get("command") == "echo hi"
