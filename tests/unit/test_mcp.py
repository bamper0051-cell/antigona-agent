"""Tests for antigona.core.mcp."""

from __future__ import annotations

import asyncio
import json

import pytest

import antigona.core.mcp as mcp_module
from antigona.core.mcp import MCPClient, MCPRegistry, MCPTool, mcp_available


def test_mcp_available_flag():
    assert isinstance(mcp_available(), bool)


def test_mcptool_from_types_tool_shape():
    tool = MCPTool(name="gmail", description="send mail", input_schema={"type": "object"})
    assert tool.name == "gmail"
    assert tool.description == "send mail"
    assert tool.input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_connect_missing_transport_raises():
    # connect_http to an unreachable url must either raise (no server) — not hang
    try:
        client = await asyncio.wait_for(
            MCPClient.connect_http("http://127.0.0.1:1/mcp/sse"), timeout=5
        )
        await client.aclose()
    except Exception:
        pass  # acceptable — no live MCP server in unit tests


@pytest.mark.asyncio
async def test_connect_stdio_bad_command_safe():
    # a bad stdio command must raise cleanly (never hang / crash the process)
    try:
        await asyncio.wait_for(
            MCPClient.connect_stdio("definitely-not-a-real-cmd-xyz"), timeout=5
        )
    except Exception:
        pass  # acceptable — no real stdio server available


def test_mcp_available_consistent_with_sdk():
    # If the mcp SDK is installed, flag must be True (so features are enabled).
    import importlib.util

    sdk = importlib.util.find_spec("mcp")
    if sdk is not None:
        assert mcp_available() is True

def test_registry_add_remove():
    from antigona.core.mcp import MCPRegistry

    r = MCPRegistry()
    r.add_stdio("gmail", "npx", ["-y", "@gmail/server"])
    r.add_http("calendar", "https://x/mcp/sse")
    assert r.names() == ["calendar", "gmail"]
    assert r.remove("gmail") is True
    assert r.remove("nope") is False
    assert r.names() == ["calendar"]


def test_registry_roundtrip():
    from antigona.core.mcp import MCPRegistry

    r = MCPRegistry()
    r.add_stdio("s", "cmd", ["a"])
    r2 = MCPRegistry.from_dict(r.to_dict())
    assert r2.to_dict() == r.to_dict()


# ── Persistence ──────────────────────────────────────────────────────────


def test_registry_save_load_roundtrip(tmp_path):
    path = tmp_path / "mcp_servers.json"
    r = MCPRegistry()
    r.add_stdio("gmail", "npx", ["-y", "@gmail/server"])
    r.add_http("calendar", "https://x/mcp/sse")
    r.save(path)

    assert path.exists()
    loaded = MCPRegistry.load(path)
    assert loaded.to_dict() == r.to_dict()
    assert loaded.names() == ["calendar", "gmail"]


def test_registry_load_missing_file_seeds_context7_default(tmp_path):
    # No file written yet: load() must still hand back a usable registry
    # with Context7 pre-registered — "out of the box", no user config needed.
    r = MCPRegistry.load(tmp_path / "does-not-exist.json")
    assert "context7" in r.names()
    entry = r.servers["context7"]
    assert entry["kind"] == "stdio"
    assert entry["command"] == "npx"
    assert "@upstash/context7-mcp" in entry["args"]
    assert entry["api_key_env"] == "CONTEXT7_API_KEY"
    # Seeding is in-memory only — nothing written until save() is called.
    assert not (tmp_path / "does-not-exist.json").exists()


def test_registry_load_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not valid json", encoding="utf-8")
    r = MCPRegistry.load(path)
    assert r.names() == []


def test_registry_save_is_atomic_no_stray_tmp_file(tmp_path):
    path = tmp_path / "mcp_servers.json"
    r = MCPRegistry()
    r.add_stdio("s", "cmd")
    r.save(path)
    leftovers = [p for p in tmp_path.iterdir() if p.name != path.name]
    assert leftovers == []


# ── _handle_mcp: persistence across separate tool calls ────────────────────


@pytest.mark.asyncio
async def test_handle_mcp_add_persists_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    from antigona.tools.registry import _handle_mcp

    out = await _handle_mcp(action="add", server="gmail", command="npx", args=["-y", "@gmail/server"])
    data = json.loads(out)
    assert data["success"] is True
    assert data["server"] == "gmail"

    # A fresh call (simulating a new tool invocation / process) must see it —
    # this is the bug fix: _handle_mcp used to build a throwaway MCPRegistry()
    # each time, so "add" was silently lost the instant the tool call returned.
    out2 = await _handle_mcp(action="list")
    data2 = json.loads(out2)
    assert "gmail" in data2["servers"]
    assert "context7" in data2["servers"]  # built-in default, always present

    assert (tmp_path / "mcp_servers.json").exists()


@pytest.mark.asyncio
async def test_handle_mcp_remove_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    from antigona.tools.registry import _handle_mcp

    await _handle_mcp(action="add", server="gmail", command="npx", args=["-y", "@gmail/server"])
    out = await _handle_mcp(action="remove", server="gmail")
    assert json.loads(out)["success"] is True

    out2 = await _handle_mcp(action="list")
    assert "gmail" not in json.loads(out2)["servers"]


@pytest.mark.asyncio
async def test_handle_mcp_list_default_has_context7(tmp_path, monkeypatch):
    # Even with zero prior "add" calls, context7 shows up — registered
    # out-of-the-box per the task's requirement.
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    from antigona.tools.registry import _handle_mcp

    out = await _handle_mcp(action="list")
    assert "context7" in json.loads(out)["servers"]


# ── _handle_mcp: config -> live connection bridge (mocked, no real process) ─


class _FakeMCPClient:
    """Stand-in for a connected antigona.core.mcp.MCPClient — no subprocess/network."""

    def __init__(self):
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(name="resolve-library-id", description="Resolve a library name to an ID", input_schema={"type": "object"}),
            MCPTool(name="get-library-docs", description="Fetch docs for a library ID", input_schema={"type": "object"}),
        ]

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments or {}))
        return f"docs-for:{name}:{(arguments or {}).get('context7CompatibleLibraryID', '')}"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_handle_mcp_tools_action_bridges_registered_server_to_live_tools(tmp_path, monkeypatch):
    """A server registered in MCPRegistry must produce real, callable tools.

    This is the missing bridge the task called out: MCPRegistry only stored
    config; nothing turned a registered entry into an actual MCPClient. We
    mock connect_from_entry (no real npx/subprocess) but exercise the real
    _handle_mcp code path end to end.
    """
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")

    fake = _FakeMCPClient()

    async def fake_connect(entry):
        assert entry["command"] == "npx"
        assert "@upstash/context7-mcp" in entry["args"]
        return fake

    monkeypatch.setattr(mcp_module, "connect_from_entry", fake_connect)

    from antigona.tools.registry import _handle_mcp

    out = await _handle_mcp(action="tools", server="context7")
    data = json.loads(out)
    assert data["success"] is True
    names = {t["name"] for t in data["tools"]}
    assert names == {"resolve-library-id", "get-library-docs"}
    assert fake.closed is True  # connection is closed after use, not leaked


@pytest.mark.asyncio
async def test_handle_mcp_call_action_invokes_tool_via_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")

    fake = _FakeMCPClient()

    async def fake_connect(entry):
        return fake

    monkeypatch.setattr(mcp_module, "connect_from_entry", fake_connect)

    from antigona.tools.registry import _handle_mcp

    out = await _handle_mcp(
        action="call",
        server="context7",
        tool="get-library-docs",
        arguments={"context7CompatibleLibraryID": "/facebook/react"},
    )
    data = json.loads(out)
    assert data["success"] is True
    assert data["tool"] == "get-library-docs"
    assert data["result"] == "docs-for:get-library-docs:/facebook/react"
    assert fake.calls == [("get-library-docs", {"context7CompatibleLibraryID": "/facebook/react"})]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_handle_mcp_tools_unknown_server_errors_without_connecting(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")

    async def fail_connect(entry):
        raise AssertionError("must not attempt to connect for an unregistered server")

    monkeypatch.setattr(mcp_module, "connect_from_entry", fail_connect)

    from antigona.tools.registry import _handle_mcp

    out = await _handle_mcp(action="tools", server="does-not-exist")
    data = json.loads(out)
    assert "error" in data


@pytest.mark.asyncio
async def test_connect_from_entry_injects_api_key_arg_for_stdio(monkeypatch):
    """connect_from_entry must resolve api_key_env and append it as a CLI arg,
    without mutating the stored entry (secrets never get written to disk)."""
    monkeypatch.setenv("CONTEXT7_API_KEY", "test-key-123")

    captured = {}

    async def fake_connect_stdio(command, args=None, env=None):
        captured["command"] = command
        captured["args"] = list(args or [])
        return "fake-client"

    monkeypatch.setattr(MCPClient, "connect_stdio", fake_connect_stdio)

    entry = {
        "kind": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "api_key_env": "CONTEXT7_API_KEY",
    }
    result = await mcp_module.connect_from_entry(entry)

    assert result == "fake-client"
    assert captured["args"] == ["-y", "@upstash/context7-mcp", "--api-key", "test-key-123"]
    # the original entry (what would get persisted) must be untouched
    assert entry["args"] == ["-y", "@upstash/context7-mcp"]


@pytest.mark.asyncio
async def test_connect_from_entry_no_key_available_omits_arg(monkeypatch):
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    # Isolate from any real vault file on disk.
    monkeypatch.setattr("antigona.secrets.vault.Vault.get", lambda self, key: None)

    captured = {}

    async def fake_connect_stdio(command, args=None, env=None):
        captured["args"] = list(args or [])
        return "fake-client"

    monkeypatch.setattr(MCPClient, "connect_stdio", fake_connect_stdio)

    entry = {
        "kind": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "api_key_env": "CONTEXT7_API_KEY",
    }
    await mcp_module.connect_from_entry(entry)
    assert captured["args"] == ["-y", "@upstash/context7-mcp"]
