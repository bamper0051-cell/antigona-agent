"""Step 2 Ollama Integration Tests.

Validates:
1. Ollama provider profile registration (base_url="http://127.0.0.1:11434/v1", model="qwen3.5:4b", local=True, auth_type="none").
2. Active provider resolution in provider_setup.py via ANTIGONA_PROVIDER="ollama".
3. Seamless integration with DialogueEngine.
4. Tool execution safety via UnifiedToolExecutionLayer.
5. Provider switching to Ollama via provider_switcher and key_manager.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import antigona.tools.key_manager as km
import antigona.tools.provider_switcher as ps
from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.conversation.provider_setup import get_default_provider
from antigona.providers.openai_compatible import OpenAICompatibleProvider
from antigona.providers.profiles import get_profile_registry


@pytest.fixture(autouse=True)
def _isolate_provider_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the persistent provider-state file so tests stay hermetic."""
    from antigona.providers.resolver import _state_file_path

    sp = _state_file_path()
    old = sp.read_text(encoding="utf-8") if sp.exists() else None
    if old is not None:
        sp.unlink()
    yield
    if old is not None:
        try:
            sp.write_text(old, encoding="utf-8")
        except OSError:
            pass
    elif sp.exists():
        try:
            sp.unlink()
        except OSError:
            pass


def test_ollama_profile_definition() -> None:
    """Test that the ollama profile is registered with correct metadata."""
    registry = get_profile_registry()
    profile = registry.get("ollama")
    assert profile is not None
    assert profile.name == "ollama"
    assert profile.display_name == "Ollama"
    assert profile.base_url == "http://127.0.0.1:11434/v1"
    assert profile.default_model == "qwen3.5:4b"
    assert profile.auth_type == "none"
    assert profile.local is True
    assert profile.env_vars == {}
    assert profile.requires_env == []

    d = profile.to_dict()
    assert d["local"] is True
    assert d["auth_type"] == "none"

    provider = profile.create_provider()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._base_url == "http://127.0.0.1:11434/v1"
    assert provider._model == "qwen3.5:4b"
    assert provider._api_key == ""


def test_provider_setup_active_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test get_default_provider resolves Ollama when ANTIGONA_PROVIDER=ollama."""
    monkeypatch.setenv("ANTIGONA_PROVIDER", "ollama")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("ANTIGONA_MODEL", raising=False)
    monkeypatch.delenv("ANTIGONA_MODEL_PRIMARY", raising=False)

    provider = get_default_provider()
    assert provider is not None
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._base_url == "http://127.0.0.1:11434/v1"
    assert provider._model == "qwen3.5:4b"
    assert provider._api_key == ""


def test_provider_setup_ollama_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test OLLAMA_MODEL environment variable overrides the default model."""
    monkeypatch.setenv("ANTIGONA_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")

    provider = get_default_provider()
    assert provider is not None
    assert provider._model == "llama3.2:3b"


@pytest.mark.asyncio
async def test_dialogue_engine_seamless_ollama_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test DialogueEngine seamlessly uses Ollama when configured as active provider."""
    monkeypatch.setenv("ANTIGONA_PROVIDER", "ollama")

    with patch.object(OpenAICompatibleProvider, "generate", return_value="Привет! Я работаю через Ollama.") as mock_gen:
        mock_repo = MagicMock()
        mock_repo.db._conn = MagicMock()
        mock_repo.session_exists = AsyncMock(return_value=True)
        mock_repo.get_messages = AsyncMock(return_value=[])
        mock_repo.add_message = AsyncMock(return_value=None)

        engine = DialogueEngine(repository=mock_repo)
        reply = await engine.reply("Привет")

        assert reply == "Привет! Я работаю через Ollama."
        mock_gen.assert_called_once()


@pytest.mark.asyncio
async def test_tool_execution_safety_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test LLM output with tool call goes through UnifiedToolExecutionLayer safety gate."""
    monkeypatch.setenv("ANTIGONA_PROVIDER", "ollama")

    mock_repo = MagicMock()
    mock_repo.db._conn = MagicMock()
    mock_repo.session_exists = AsyncMock(return_value=True)
    mock_repo.get_messages = AsyncMock(return_value=[])
    mock_repo.add_message = AsyncMock(return_value=None)

    mock_registry = MagicMock()
    tool_resp = (
        chr(0x27ea)
        + "tool:kanban action=\"create\" title=\"Ollama Task\""
        + chr(0x27eb)
    )

    with patch.object(OpenAICompatibleProvider, "generate", return_value=tool_resp):
        with patch("antigona.engine.unified_executor.UnifiedToolExecutionLayer.execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "Task created OK"

            engine = DialogueEngine(repository=mock_repo, registry=mock_registry)
            reply = await engine.reply("Создай задачу")

            mock_exec.assert_called_once()
            req = mock_exec.call_args[0][0]
            assert req.tool_name == "kanban"
            assert req.params == {"action": "create", "title": "Ollama Task"}
            assert "Инструмент `kanban` выполнен" in reply


def test_key_manager_detect_and_apply_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test key_manager functions for Ollama."""
    assert km.detect_provider("ollama") == "ollama"
    assert km.detect_provider("http://127.0.0.1:11434/v1") == "ollama"

    ok, msg = km.apply_provider("ollama")
    assert ok is True
    assert "Ollama" in msg
    assert os.environ.get("ANTIGONA_PROVIDER") == "ollama"


def test_provider_switcher_ollama_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test provider_switcher functions for Ollama."""
    with patch.object(OpenAICompatibleProvider, "generate", return_value="OK"):
        ok, msg = ps.switch_to_provider("ollama")
        assert ok is True
        assert "Ollama" in msg

    providers = ps.get_available_providers()
    ollama_info = next((p for p in providers if p["name"] == "ollama"), None)
    assert ollama_info is not None
    assert ollama_info["display_name"] == "Ollama"
    assert ollama_info["base_url"] == "http://127.0.0.1:11434/v1"

    with patch.object(OpenAICompatibleProvider, "generate", return_value="OK"):
        ok, msg = ps.test_current_provider()
        assert ok is True
        assert "Provider OK" in msg


def test_cli_provider_model_commands_parsing():
    from antigona.cli_ui.commands import CommandKind, parse_command

    p1 = parse_command("/setllm ollama qwen3.5:4b")
    assert p1.kind == CommandKind.SETLLM
    assert p1.args == ("ollama", "qwen3.5:4b")

    p2 = parse_command("/provider deepseek")
    assert p2.kind == CommandKind.PROVIDER
    assert p2.args == ("deepseek",)

    p3 = parse_command("/model qwen3:4b")
    assert p3.kind == CommandKind.MODEL
    assert p3.args == ("qwen3:4b",)

