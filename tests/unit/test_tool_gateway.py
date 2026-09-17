"""Tests for the Tool Gateway — per-tool backend selection.

Tests cover:
- ToolGatewayConfig: save/load, defaults, serialization round-trip
- GatewayRouter: resolve, list_tools, mutation
- Singleton get_gateway / reset_gateway
- Command parsing in handle_tool_gateway_command
- Status table formatting
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from antigona.gateway.tool_gateway import (
    Backend,
    GatewayRouter,
    ToolConfig,
    ToolGatewayConfig,
    ToolName,
    get_gateway,
    handle_tool_gateway_command,
    reset_gateway,
)

# ─── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    """Return a temporary path for the gateway config JSON."""
    return tmp_path / ".antigona" / "gateway_config.json"


@pytest.fixture
def default_config() -> ToolGatewayConfig:
    return ToolGatewayConfig()


@pytest.fixture
def router(tmp_config_path: Path) -> GatewayRouter:
    return GatewayRouter(config_path=tmp_config_path)


# ─── ToolConfig ────────────────────────────────────────────────────────────────


class TestToolConfig:
    def test_default_use_gateway(self) -> None:
        cfg = ToolConfig(backend="duckduckgo")
        assert cfg.backend == "duckduckgo"
        assert cfg.use_gateway is True

    def test_explicit_use_gateway(self) -> None:
        cfg = ToolConfig(backend="openai", use_gateway=False)
        assert cfg.backend == "openai"
        assert cfg.use_gateway is False

    def test_to_dict(self) -> None:
        cfg = ToolConfig(backend="pollinations", use_gateway=True)
        assert cfg.to_dict() == {"backend": "pollinations", "use_gateway": True}

    def test_from_dict(self) -> None:
        cfg = ToolConfig.from_dict({"backend": "openai", "use_gateway": False})
        assert cfg.backend == "openai"
        assert cfg.use_gateway is False

    def test_from_dict_missing_keys(self) -> None:
        cfg = ToolConfig.from_dict({})
        assert cfg.backend == ""
        assert cfg.use_gateway is True  # default


# ─── ToolGatewayConfig ─────────────────────────────────────────────────────────


class TestToolGatewayConfig:
    def test_defaults_all_tools_present(self, default_config: ToolGatewayConfig) -> None:
        """Default config should contain all four tools."""
        assert set(default_config.tools.keys()) == {
            ToolName.WEB, ToolName.IMAGE_GEN, ToolName.TTS, ToolName.BROWSER,
        }

    def test_default_backends(self, default_config: ToolGatewayConfig) -> None:
        assert default_config.get_tool("web").backend == Backend.DUCKDUCKGO
        assert default_config.get_tool("image_gen").backend == Backend.POLLINATIONS
        assert default_config.get_tool("tts").backend == Backend.EDGE_TTS
        assert default_config.get_tool("browser").backend == Backend.PLAYWRIGHT

    def test_get_tool_unknown(self, default_config: ToolGatewayConfig) -> None:
        from antigona.gateway.tool_gateway import UnknownToolError
        with pytest.raises(UnknownToolError):
            default_config.get_tool("nonexistent")

    def test_set_backend(self, default_config: ToolGatewayConfig) -> None:
        default_config.set_backend("web", "firecrawl")
        assert default_config.get_tool("web").backend == "firecrawl"

    def test_set_backend_invalid(self, default_config: ToolGatewayConfig) -> None:
        """Should raise ValueError for unsupported backend."""
        with pytest.raises(ValueError, match="not supported"):
            default_config.set_backend("web", "openai")  # openai not valid for web

    def test_set_backend_unknown_tool(self, default_config: ToolGatewayConfig) -> None:
        from antigona.gateway.tool_gateway import UnknownToolError
        with pytest.raises(UnknownToolError):
            default_config.set_backend("nonexistent", "duckduckgo")

    def test_set_use_gateway(self, default_config: ToolGatewayConfig) -> None:
        default_config.set_use_gateway("web", False)
        assert default_config.get_tool("web").use_gateway is False

    def test_set_use_gateway_unknown(self, default_config: ToolGatewayConfig) -> None:
        from antigona.gateway.tool_gateway import UnknownToolError
        with pytest.raises(UnknownToolError):
            default_config.set_use_gateway("nonexistent", False)

    def test_reset_to_defaults(self, default_config: ToolGatewayConfig) -> None:
        default_config.set_backend("web", "firecrawl")
        default_config.reset_to_defaults()
        assert default_config.get_tool("web").backend == Backend.DUCKDUCKGO

    def test_to_dict_round_trip(self, default_config: ToolGatewayConfig) -> None:
        data = default_config.to_dict()
        restored = ToolGatewayConfig.from_dict(data)
        assert restored.tools.keys() == default_config.tools.keys()
        for name in default_config.tools:
            assert restored.get_tool(name).backend == default_config.get_tool(name).backend
            assert restored.get_tool(name).use_gateway == default_config.get_tool(name).use_gateway

    def test_save_and_load(self, tmp_config_path: Path) -> None:
        """Save config to file, load it back, verify contents."""
        config = ToolGatewayConfig()
        config.set_backend("web", "firecrawl")
        config.save(tmp_config_path)

        assert tmp_config_path.exists()
        loaded = ToolGatewayConfig.load(tmp_config_path)
        assert loaded.get_tool("web").backend == "firecrawl"
        assert loaded.get_tool("image_gen").backend == Backend.POLLINATIONS  # unchanged

    def test_load_missing_file(self, tmp_config_path: Path) -> None:
        """Loading a missing file should return defaults, not error."""
        config = ToolGatewayConfig.load(tmp_config_path)
        assert config.get_tool("web").backend == Backend.DUCKDUCKGO

    def test_load_corrupted_file(self, tmp_config_path: Path) -> None:
        """Loading corrupted JSON should return defaults, not crash."""
        tmp_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_config_path.write_text("{invalid json")
        config = ToolGatewayConfig.load(tmp_config_path)
        assert config.get_tool("web").backend == Backend.DUCKDUCKGO


# ─── GatewayRouter ─────────────────────────────────────────────────────────────


class TestGatewayRouter:
    def test_resolve_default(self, router: GatewayRouter) -> None:
        config, use_gateway = router.resolve("web")
        assert config.backend == Backend.DUCKDUCKGO
        assert use_gateway is True

    def test_resolve_after_backend_change(self, router: GatewayRouter) -> None:
        router.set_backend("image_gen", "openai")
        config, use_gateway = router.resolve("image_gen")
        assert config.backend == "openai"
        assert use_gateway is True

    def test_resolve_use_gateway_off(self, router: GatewayRouter) -> None:
        router.set_use_gateway("tts", False)
        config, use_gateway = router.resolve("tts")
        assert use_gateway is False

    def test_resolve_unknown(self, router: GatewayRouter) -> None:
        from antigona.gateway.tool_gateway import UnknownToolError
        with pytest.raises(UnknownToolError):
            router.resolve("nonexistent")

    def test_list_tools(self, router: GatewayRouter) -> None:
        tools = router.list_tools()
        assert set(tools.keys()) == {"web", "image_gen", "tts", "browser"}
        assert tools["web"]["backend"] == Backend.DUCKDUCKGO
        assert tools["web"]["use_gateway"] is True
        assert tools["web"]["status"] == "on"

    def test_list_tools_after_change(self, router: GatewayRouter) -> None:
        router.set_use_gateway("tts", False)
        tools = router.list_tools()
        assert tools["tts"]["use_gateway"] is False
        assert tools["tts"]["status"] == "off"

    def test_reset(self, router: GatewayRouter) -> None:
        router.set_backend("web", "firecrawl")
        router.reset()
        config, _ = router.resolve("web")
        assert config.backend == Backend.DUCKDUCKGO

    def test_save_on_mutation(self, tmp_config_path: Path) -> None:
        """set_backend should persist to disk."""
        router = GatewayRouter(config_path=tmp_config_path)
        router.set_backend("web", "firecrawl")
        assert tmp_config_path.exists()
        data = json.loads(tmp_config_path.read_text())
        assert data["tools"]["web"]["backend"] == "firecrawl"

    def test_load_from_existing(self, tmp_config_path: Path) -> None:
        """A new router should load saved state."""
        router1 = GatewayRouter(config_path=tmp_config_path)
        router1.set_backend("web", "firecrawl")
        router2 = GatewayRouter(config_path=tmp_config_path)
        config, _ = router2.resolve("web")
        assert config.backend == "firecrawl"


# ─── Singleton ─────────────────────────────────────────────────────────────────


class TestSingleton:
    def setup_method(self) -> None:
        reset_gateway()

    def test_get_gateway_returns_same_instance(self) -> None:
        g1 = get_gateway()
        g2 = get_gateway()
        assert g1 is g2

    def test_reset_gateway(self) -> None:
        g1 = get_gateway()
        reset_gateway()
        g2 = get_gateway()
        assert g1 is not g2

    def test_gateway_is_router(self) -> None:
        g = get_gateway()
        assert isinstance(g, GatewayRouter)


# ─── Command parsing (handle_tool_gateway_command) ─────────────────────────────


class TestHandleCommand:
    def test_no_args_shows_status(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command([], router)
        assert "Tool Gateway" in reply
        assert "duckduckgo" in reply
        assert "pollinations" in reply

    def test_status_subcommand(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["status"], router)
        assert "Tool Gateway" in reply
        assert "Gateway on" in reply

    def test_help_subcommand(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["help"], router)
        assert "/tool-gateway" in reply
        assert "переключить бэкенд" in reply

    def test_show_single_tool(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["web"], router)
        assert "Web" in reply or "web" in reply
        assert "duckduckgo" in reply

    def test_set_backend(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["web", "backend", "firecrawl"], router)
        assert "✅" in reply
        assert "firecrawl" in reply
        # Verify persisted
        config, _ = router.resolve("web")
        assert config.backend == "firecrawl"

    def test_set_backend_invalid(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["web", "backend", "openai"], router)
        assert "❌" in reply
        assert "not supported" in reply

    def test_set_backend_missing_arg(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["web", "backend"], router)
        assert "❌" in reply

    def test_set_use_gateway_true(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["tts", "use_gateway", "true"], router)
        assert "✅" in reply
        assert router.config.get_tool("tts").use_gateway is True

    def test_set_use_gateway_false(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["tts", "use_gateway", "false"], router)
        assert "✅" in reply
        assert router.config.get_tool("tts").use_gateway is False

    def test_set_use_gateway_invalid_flag(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["tts", "use_gateway", "maybe"], router)
        assert "❌" in reply

    def test_reset_command(self, router: GatewayRouter) -> None:
        router.set_backend("web", "firecrawl")
        reply = handle_tool_gateway_command(["reset"], router)
        assert "сброшена" in reply
        assert router.config.get_tool("web").backend == Backend.DUCKDUCKGO

    def test_unknown_tool(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["nonexistent"], router)
        assert "❌" in reply

    def test_unknown_action(self, router: GatewayRouter) -> None:
        reply = handle_tool_gateway_command(["web", "bogus"], router)
        assert "❌" in reply

    def test_tool_alias_image(self, router: GatewayRouter) -> None:
        """'image' should alias to 'image_gen'."""
        reply = handle_tool_gateway_command(["image", "backend", "openai"], router)
        assert "✅" in reply
        config, _ = router.resolve("image_gen")
        assert config.backend == "openai"

    def test_tool_alias_voice(self, router: GatewayRouter) -> None:
        """'voice' should alias to 'tts'."""
        reply = handle_tool_gateway_command(["voice", "use_gateway", "false"], router)
        assert "✅" in reply
        assert router.config.get_tool("tts").use_gateway is False

    def test_tool_alias_search(self, router: GatewayRouter) -> None:
        """'search' should alias to 'web'."""
        reply = handle_tool_gateway_command(["search", "backend", "firecrawl"], router)
        assert "✅" in reply
        config, _ = router.resolve("web")
        assert config.backend == "firecrawl"

    def test_use_gateway_alternative_names(self, router: GatewayRouter) -> None:
        """Test 'use-gateway' and 'gateway' action aliases."""
        reply = handle_tool_gateway_command(["web", "use-gateway", "true"], router)
        assert "✅" in reply
        assert router.config.get_tool("web").use_gateway is True

        reply = handle_tool_gateway_command(["web", "gateway", "false"], router)
        assert "✅" in reply
        assert router.config.get_tool("web").use_gateway is False

    def test_parse_flag_variants(self, router: GatewayRouter) -> None:
        """Test various boolean flag synonyms."""
        for flag in ("true", "on", "yes", "1", "да"):
            handle_tool_gateway_command(["web", "use_gateway", flag], router)
            assert router.config.get_tool("web").use_gateway is True, f"Expected True for '{flag}'"

        handle_tool_gateway_command(["web", "use_gateway", "no"], router)
        assert router.config.get_tool("web").use_gateway is False


# ─── Integration with ActionExecutor (_get_backend_for) ────────────────────────


class TestActionExecutorIntegration:
    def test_get_backend_for_image_gen_returns_generator(self, router: GatewayRouter) -> None:
        """With default config, _get_backend_for('image_gen') should return ImageGenerator."""
        from antigona.tools.action_executor import ActionExecutor
        from antigona.tools.image_gen import ImageGenerator

        executor = ActionExecutor()
        with patch("antigona.gateway.tool_gateway.get_gateway", return_value=router):
            cls = executor._get_backend_for("image_gen")  # noqa: SLF001
        assert cls is ImageGenerator

    def test_get_backend_for_unknown_tool_returns_none(self, router: GatewayRouter) -> None:
        """_get_backend_for an unknown tool returns None."""
        from antigona.tools.action_executor import ActionExecutor

        executor = ActionExecutor()
        with patch("antigona.gateway.tool_gateway.get_gateway", return_value=router):
            result = executor._get_backend_for("nonexistent")  # noqa: SLF001
        assert result is None

    def test_get_backend_for_use_gateway_false_falls_back(self, router: GatewayRouter) -> None:
        """When use_gateway is False, fall back to default."""
        from antigona.tools.action_executor import ActionExecutor
        from antigona.tools.image_gen import ImageGenerator

        router.set_use_gateway("image_gen", False)
        executor = ActionExecutor()
        with patch("antigona.gateway.tool_gateway.get_gateway", return_value=router):
            cls = executor._get_backend_for("image_gen")  # noqa: SLF001
        assert cls is ImageGenerator  # falls back to default


# ─── Format helpers ────────────────────────────────────────────────────────────


class TestFormatStatusTable:
    def test_contains_all_tools(self, router: GatewayRouter) -> None:
        from antigona.gateway.tool_gateway import format_status_table

        tools = router.list_tools()
        table = format_status_table(tools)
        assert "Web" in table
        assert "Image Gen" in table
        assert "TTS" in table
        assert "Browser" in table
        assert "Gateway on" in table

    def test_reflects_off_state(self, router: GatewayRouter) -> None:
        from antigona.gateway.tool_gateway import format_status_table

        router.set_use_gateway("tts", False)
        tools = router.list_tools()
        table = format_status_table(tools)
        assert "Gateway off" in table
