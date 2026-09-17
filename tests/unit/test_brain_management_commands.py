"""Regression guard: /install, /mcp, /plugins, /cli route through the brain.

These are server-routed management commands that must:
- router: classify to command.install / command.mcp / command.plugins / command.cli
- brain: dispatch deterministic handlers (never the generic LLM dialogue)
- CLI: parse to typed CommandKind (never UNKNOWN) and forward via the turn
  API (never submit_task)
- Telegram: registered as known commands (thin transport to the Turn API)

Proven pattern from the CLI Chat OA: a command the brain owns must not be
swallowed by the CLI's local UNKNOWN handler, and must not create a task flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.commands import (
    CommandDisposition,
    CommandKind,
    parse_command,
)
from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.router.intent_router import IntentRouter

# ── Router classification ─────────────────────────────────────────────────────


@pytest.fixture()
def router() -> IntentRouter:
    return IntentRouter()


@pytest.mark.parametrize(
    "cmd,intent",
    [
        ("/install pip six", "command.install"),
        ("/mcp", "command.mcp"),
        ("/mcp list", "command.mcp"),
        ("/plugins", "command.plugins"),
        ("/plugins list", "command.plugins"),
        ("/cli", "command.cli"),
    ],
)
def test_management_commands_route_to_own_intent(router: IntentRouter, cmd: str, intent: str) -> None:
    d = router.route(cmd, context={})
    assert d.intent == intent, f"{cmd} routed to {d.intent}, expected {intent}"


def test_management_commands_not_help(router: IntentRouter) -> None:
    for cmd in ["/mcp", "/plugins", "/cli", "/install"]:
        d = router.route(cmd, context={})
        assert d.intent != "command.help", f"{cmd} fell through to help"


# ── CLI parse_command: typed kinds (never UNKNOWN) ────────────────────────────


@pytest.mark.parametrize(
    "raw,expected_kind",
    [
        ("/install pip six", CommandKind.INSTALL),
        ("/mcp", CommandKind.MCP),
        ("/mcp list", CommandKind.MCP),
        ("/plugins", CommandKind.PLUGINS),
        ("/plugins list", CommandKind.PLUGINS),
        ("/cli", CommandKind.CLI),
    ],
)
def test_cli_parse_management_commands(raw: str, expected_kind: CommandKind) -> None:
    result = parse_command(raw)
    assert result.kind == expected_kind, f"{raw!r} parsed to {result.kind}, expected {expected_kind}"
    assert result.kind is not CommandKind.UNKNOWN


def test_cli_parse_install_keeps_args() -> None:
    result = parse_command("/install npm lodash")
    assert result.kind == CommandKind.INSTALL
    assert result.args == ("npm", "lodash")


# ── CLI handle_input: forward to turn API, never submit_task ─────────────────


class _TrackedTurnGateway:
    """GatewayClientProtocol mock tracking turn + submit_task calls."""

    def __init__(self) -> None:
        self.turn_calls: list[str] = []
        self.submit_calls: list[str] = []

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> Any:
        self.turn_calls.append(text)
        return {"reply": "ok", "response_type": "conversation", "session_id": session_id}

    async def submit_task(self, **kwargs: Any) -> Any:
        self.submit_calls.append(str(kwargs.get("message", "")))
        return {"id": "mock-flow", "status": "QUEUED"}


@pytest.mark.parametrize(
    "raw",
    ["/install pip six", "/mcp list", "/plugins list", "/cli"],
)
@pytest.mark.asyncio
async def test_cli_management_commands_forward_to_turn_not_submit(raw: str) -> None:
    mock = _TrackedTurnGateway()
    ctrl = ChatController(gateway=mock)
    result = await ctrl.handle_input(raw)
    assert mock.turn_calls == [raw], f"expected turn call for {raw!r}, got {mock.turn_calls}"
    assert mock.submit_calls == [], "management commands must never call submit_task"
    assert result == CommandDisposition.LOCAL_ACTION


# ── Brain handlers ────────────────────────────────────────────────────────────


@pytest.fixture()
def brain(tmp_path: Path) -> AntigonaBrain:
    return AntigonaBrain(db_path=str(tmp_path / "test.db"))


@pytest.mark.asyncio
async def test_brain_dispatch_mcp(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    import antigona.core.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    from antigona.router.intent_router import IntentDecision

    decision = IntentDecision(intent="command.mcp", confidence=0.99, response_mode="command_result")
    resp = await brain._handle_command("/mcp list", "s", decision)
    assert resp.intent == "command.mcp"
    assert resp.response_type in (ResponseType.CONVERSATION, ResponseType.ERROR)


@pytest.mark.asyncio
async def test_brain_mcp_add_remove_persists(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    import antigona.core.mcp as mcp_module

    registry_path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", registry_path)

    add = await brain._command_mcp("/mcp add gmail npx -y @gmail/server")
    assert add.response_type == ResponseType.CONVERSATION
    assert "gmail" in add.text

    assert registry_path.exists()
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "gmail" in data

    listing = await brain._command_mcp("/mcp list")
    assert "gmail" in listing.text

    remove = await brain._command_mcp("/mcp remove gmail")
    assert "удалён" in remove.text
    listing2 = await brain._command_mcp("/mcp list")
    assert "gmail" not in listing2.text


@pytest.mark.asyncio
async def test_brain_mcp_remove_missing(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    import antigona.core.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    resp = await brain._command_mcp("/mcp remove nope")
    assert "не найден" in resp.text


@pytest.mark.asyncio
async def test_brain_cli_handler(brain: AntigonaBrain) -> None:
    resp = brain._command_cli()
    assert resp.intent == "command.cli"
    assert resp.response_type == ResponseType.CONVERSATION
    assert any(cmd in resp.text for cmd in ("/install", "/mcp", "/plugins", "/cli"))


@pytest.mark.asyncio
async def test_brain_plugins_list_empty(brain: AntigonaBrain) -> None:
    # No ~/.antigona/plugins on the host → discover returns [] → the handler
    # reports "not found" deterministically (no crash, correct intent).
    resp = await brain._command_plugins("/plugins list")
    assert resp.intent == "command.plugins"
    assert resp.response_type in (ResponseType.CONVERSATION, ResponseType.ERROR)


@pytest.mark.asyncio
async def test_brain_plugins_unload_missing(brain: AntigonaBrain, monkeypatch: Any) -> None:
    resp = await brain._command_plugins("/plugins unload nope")
    assert "не найден" in resp.text


# ── Stage 5.5 rework regression tests (C1/C2/H1/H2) ──────────────────────────


@pytest.mark.asyncio
async def test_mcp_add_http_url_stored_as_http(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    """H1: /mcp add <name> <https-url> must persist kind=http, never stdio."""
    import antigona.core.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    resp = await brain._command_mcp("/mcp add calendar https://example.com/mcp")
    assert "HTTP" in resp.text
    assert "https://example.com/mcp" in resp.text
    data = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    assert data["calendar"]["kind"] == "http"
    assert data["calendar"]["url"] == "https://example.com/mcp"


@pytest.mark.asyncio
async def test_mcp_add_stdio_command_stored_as_stdio(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    """H1: a non-http target is still stored as a stdio command."""
    import antigona.core.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_REGISTRY_PATH", tmp_path / "mcp_servers.json")
    resp = await brain._command_mcp("/mcp add gmail npx -y @gmail/server")
    assert "HTTP" not in resp.text
    data = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    assert data["gmail"]["kind"] == "stdio"
    assert data["gmail"]["command"] == "npx"


def _write_plugin(root: Path, name: str) -> Path:
    """Create a minimal loadable plugin (plugin.yaml + init.py) under root/name."""
    pdir = root / "plugins" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.yaml").write_text(
        f"name: {name}\nkind: tool\nversion: 0.1.0\n", encoding="utf-8"
    )
    (pdir / "init.py").write_text(
        "def register(ctx):\n    ctx.register_tool("
        '{"name": "ping", "description": "p", "parameters": {"type": "object"}}, '
        "lambda: 'pong')\n",
        encoding="utf-8",
    )
    return pdir


@pytest.mark.asyncio
async def test_plugins_load_then_unload_roundtrip(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    """H2: plugin registry must be shared across calls — load then unload works."""
    import antigona.core.paths as paths

    root = tmp_path / "owner"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    _write_plugin(root, "demo")
    monkeypatch.setattr(paths, "owner_dir", lambda: root)

    load = await brain._command_plugins("/plugins load demo")
    assert "загружен" in load.text, load.text
    unload = await brain._command_plugins("/plugins unload demo")
    assert "выгружен" in unload.text, unload.text


@pytest.mark.asyncio
async def test_plugins_unload_survives_a_subsequent_listing(
    brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any
) -> None:
    """PLUG-RELOAD-01: unloading must not be undone by merely listing plugins.

    ``/plugins`` (list) calls ``PluginLoader.load_all()``, which re-discovers every
    directory under the plugins root. Without a durable record of what the owner
    disabled, that listing silently re-registers the plugin — so the owner is told
    "выгружен" while the plugin (and its tools/hooks) is active again.
    """
    import antigona.core.paths as paths

    root = tmp_path / ".antigona"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    _write_plugin(root, "demo")
    monkeypatch.setattr(paths, "owner_dir", lambda: root)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert "загружен" in (await brain._command_plugins("/plugins load demo")).text
    assert "выгружен" in (await brain._command_plugins("/plugins unload demo")).text

    listing = await brain._command_plugins("/plugins")
    assert "demo" not in listing.text, listing.text

    again = await brain._command_plugins("/plugins unload demo")
    assert "не найден" in again.text, again.text


@pytest.mark.asyncio
async def test_plugins_load_after_unload_reactivates(
    brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any
) -> None:
    """Disabling is durable but reversible: an explicit load clears it."""
    import antigona.core.paths as paths

    root = tmp_path / ".antigona"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    _write_plugin(root, "demo")
    monkeypatch.setattr(paths, "owner_dir", lambda: root)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    await brain._command_plugins("/plugins load demo")
    await brain._command_plugins("/plugins unload demo")
    assert "загружен" in (await brain._command_plugins("/plugins load demo")).text

    listing = await brain._command_plugins("/plugins")
    assert "demo" in listing.text, listing.text


@pytest.mark.asyncio
async def test_plugins_load_rejects_path_traversal(brain: AntigonaBrain, tmp_path: Path, monkeypatch: Any) -> None:
    """C2: a plugin name with /, .. or absolute path must be rejected, never loaded."""
    import antigona.core.paths as paths

    root = tmp_path / "owner"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    # An evil plugin OUTSIDE the plugins root (would be RCE if reachable).
    evil = tmp_path / "evil"
    evil.mkdir(parents=True, exist_ok=True)
    (evil / "plugin.yaml").write_text("name: evil\nkind: tool\n", encoding="utf-8")
    (evil / "init.py").write_text(
        "import pathlib; pathlib.Path('/tmp/pwned_marker').write_text('pwned')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "owner_dir", lambda: root)

    for bad in (f"{evil}", f"{evil}/init.py", "..", "../evil", "a/b"):
        resp = await brain._command_plugins(f"/plugins load {bad}")
        assert "загружен" not in resp.text, f"traversal {bad!r} unexpectedly loaded"
    # marker must never be written
    assert not Path("/tmp/pwned_marker").exists()
    assert not Path("/tmp/pwned_marker").is_file()


# ── Command registry: available to BOTH CLI and Telegram channels ────────────


def test_registry_exposes_management_commands_to_both_channels() -> None:
    from antigona.core.command_registry import commands_for_channel, find_command

    for name in ("install", "mcp", "plugins", "cli"):
        spec = find_command(name)
        assert spec is not None, f"/{name} not in command registry"
        assert "cli" in spec.channels, f"/{name} not exposed to cli"
        assert "telegram" in spec.channels, f"/{name} not exposed to telegram"

    cli_names = {s.name for s in commands_for_channel("cli")}
    tg_names = {s.name for s in commands_for_channel("telegram")}
    for name in ("install", "mcp", "plugins", "cli"):
        assert name in cli_names
        assert name in tg_names


def test_telegram_help_includes_management_commands() -> None:
    from antigona.channels.telegram.bot import _build_help_text

    text = _build_help_text()
    for name in ("install", "mcp", "plugins", "cli"):
        assert f"/{name}" in text, f"/{name} missing from Telegram /help"
