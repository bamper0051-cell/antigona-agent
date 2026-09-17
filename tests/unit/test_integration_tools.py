"""Tests for integration modules registered as Antigona builtin tools."""

from __future__ import annotations

import pytest

from antigona.tools.registry import ToolRegistry, register_builtins


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_builtins(reg)
    return reg


def _names(reg):
    tools = reg.list()
    return sorted(getattr(t, "name", str(t)) for t in tools)


def test_integration_tools_registered(registry):
    names = _names(registry)
    for t in ("count_tokens", "kanban", "loop", "rss", "mcp", "acp"):
        assert t in names, f"missing builtin tool: {t}"


@pytest.mark.asyncio
async def test_count_tokens_tool(registry):
    r = await registry.dispatch("count_tokens", text="привет мир")
    assert '"success": true' in r
    assert '"tokens":' in r


@pytest.mark.asyncio
async def test_kanban_tool_create(registry, tmp_path):
    r = await registry.dispatch(
        "kanban", action="create", title="тул-карточка", board=str(tmp_path / "kb")
    )
    assert '"success": true' in r
    assert '"id"' in r


@pytest.mark.asyncio
async def test_loop_tool(registry):
    r = await registry.dispatch("loop", max_iterations=3)
    assert '"success": true' in r
    assert '"status"' in r


@pytest.mark.asyncio
async def test_mcp_tool_list(registry):
    r = await registry.dispatch("mcp", action="list")
    assert '"success": true' in r


@pytest.mark.asyncio
async def test_acp_tool_list(registry):
    r = await registry.dispatch("acp", action="list")
    assert '"success": true' in r
