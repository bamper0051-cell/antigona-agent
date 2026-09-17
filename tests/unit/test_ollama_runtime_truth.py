"""Tests for Ollama Post-Install Truth & Provider Introspection.

Verifies:
1. ProviderResolver is the canonical single source of truth for /setllm, /model, ContextBuilder, and DialogueEngine.
2. Server-generated RUNTIME ENVIRONMENT block is injected into system context.
3. Provider switching dynamically updates system prompt, /model output, and actual HTTP transport.
4. Fail-closed behavior when provider is unresolved.
5. Legacy error messages are provider-neutral.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from antigona.context.builder import ContextBuilder
from antigona.conversation.dialogue_engine import DialogueEngine
from antigona.providers.resolver import ProviderResolver


@pytest.fixture(autouse=True)
def reset_provider_env():
    """Backup and restore provider environment variables around each test."""
    old_env = {
        k: os.environ.get(k)
        for k in [
            "ANTIGONA_PROVIDER",
            "ACTIVE_PROVIDER",
            "PROVIDER_BASE_URL",
            "OLLAMA_MODEL",
            "ANTIGONA_MODEL",
            "DEEPSEEK_API_KEY",
            "OPENROUTER_API_KEY",
        ]
    }
    from antigona.providers.resolver import _state_file_path
    _sp = _state_file_path()
    _old_state = _sp.read_text(encoding="utf-8") if _sp.exists() else None
    if _old_state is not None:
        _sp.unlink()
    ProviderResolver.clear_cache()
    yield
    ProviderResolver.clear_cache()
    if _old_state is not None:
        try:
            _sp.write_text(_old_state, encoding="utf-8")
        except OSError:
            pass
    elif _sp.exists():
        try:
            _sp.unlink()
        except OSError:
            pass
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_provider_resolver_ollama_active():
    """Assert ProviderResolver accurately resolves active Ollama state."""
    ProviderResolver.set_active_provider("ollama", "qwen3.5:4b")
    info = ProviderResolver.get_active_info()

    assert info.provider_name == "ollama"
    assert info.display_name == "Ollama"
    assert info.model_name == "qwen3.5:4b"
    assert info.endpoint_class == "local"
    assert info.status == "active"
    assert "11434" in info.base_url

    system_block = info.to_system_block()
    assert "--- RUNTIME ENVIRONMENT ---" in system_block
    assert "LLM provider: Ollama" in system_block
    assert "LLM model: qwen3.5:4b" in system_block
    assert "Execution endpoint class: local" in system_block
    assert "Provider status: active" in system_block


def test_context_builder_injects_runtime_environment_block():
    """Assert ContextBuilder includes the RUNTIME ENVIRONMENT block in system prompt."""
    ProviderResolver.set_active_provider("ollama", "qwen3.5:4b")

    builder = ContextBuilder()
    messages = builder.build(turn_buffer=[{"role": "user", "content": "Привет"}])

    assert len(messages) >= 2
    system_msg = messages[0]["content"]
    assert "--- RUNTIME ENVIRONMENT ---" in system_msg
    assert "LLM provider: Ollama" in system_msg
    assert "LLM model: qwen3.5:4b" in system_msg
    assert "Execution endpoint class: local" in system_msg


def test_provider_switch_updates_runtime_facts_atomically():
    """Assert /setllm switch atomically updates system context and resolver info."""
    # 1. Switch to Ollama
    success, msg = ProviderResolver.set_active_provider("ollama", "qwen3.5:4b")
    assert success
    info1 = ProviderResolver.get_active_info()
    assert info1.provider_name == "ollama"
    assert info1.model_name == "qwen3.5:4b"

    # Build prompt
    builder = ContextBuilder()
    prompt1 = builder.build()[0]["content"]
    assert "LLM provider: Ollama" in prompt1
    assert "LLM model: qwen3.5:4b" in prompt1

    # 2. Switch to DeepSeek
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake-key"
    success2, msg2 = ProviderResolver.set_active_provider("deepseek", "deepseek-chat")
    assert success2
    info2 = ProviderResolver.get_active_info()
    assert info2.provider_name == "deepseek"
    assert info2.model_name == "deepseek-chat"
    assert info2.endpoint_class == "remote"

    # Build prompt again
    prompt2 = builder.build()[0]["content"]
    assert "LLM provider: DeepSeek" in prompt2
    assert "LLM model: deepseek-chat" in prompt2
    assert "Execution endpoint class: remote" in prompt2


@pytest.mark.asyncio
async def test_dialogue_engine_routing_to_active_provider():
    """Regression test: assert DialogueEngine calls only active provider's HTTP route."""
    ProviderResolver.set_active_provider("ollama", "qwen3.5:4b")

    called_urls = []

    def mock_post(url, headers=None, json=None):
        called_urls.append(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Сейчас я работаю локально через Ollama. Модель: qwen3.5:4b."}}]
        }
        return mock_resp

    engine = DialogueEngine()
    with patch("httpx.Client.post", side_effect=mock_post):
        reply = await engine.reply("какой провайдер и модель ты сейчас используешь?", session_id="test-session")

    assert len(called_urls) == 1
    assert "11434" in called_urls[0]
    assert "deepseek" not in called_urls[0]
    assert "openrouter" not in called_urls[0]
    assert "Ollama" in reply
    assert "qwen3.5:4b" in reply


def test_fail_closed_unresolved_truth():
    """Assert unresolved provider state reports 'unresolved' and fail-closed status."""
    os.environ["ANTIGONA_PROVIDER"] = "none"

    info = ProviderResolver.get_active_info()
    assert info.status == "unresolved"
    assert info.provider_name == "unresolved"
    assert info.model_name == "unresolved"

    block = info.to_system_block()
    assert "LLM provider: unresolved" in block
    assert "Provider status: unresolved" in block


def test_neutral_error_messages():
    """Assert error messages when no provider is active are provider-neutral."""
    os.environ.pop("ANTIGONA_PROVIDER", None)
    os.environ.pop("ACTIVE_PROVIDER", None)
    os.environ.pop("PROVIDER_BASE_URL", None)
    os.environ.pop("DEEPSEEK_API_KEY", None)

    from antigona.tools.provider_switcher import test_current_provider

    with patch("antigona.providers.resolver.ProviderResolver.get_provider", return_value=None):
        ok, msg = test_current_provider()
        assert not ok
        assert "No active LLM provider is configured" in msg
        assert "DEEPSEEK_API_KEY" not in msg


# ── Split-brain regression: persistent canonical state across processes ──

@pytest.fixture(autouse=True)
def _isolate_provider_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFECT-P0-PROVIDER-STATE: _save_state пишет в project_local_dir() живого
    стека. Без изоляции unit-прогон перезаписывает боевой .antigona/provider_state.json
    (зафиксировано 2026-08-28: прогон записал openrouter/openai/gpt-4o-mini в живой
    конфиг и R4 прошёл на платной модели). Каждый тест работает со своим tmp root.
    """
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))


def test_split_brain_persist_across_process(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A /setllm switch in one process (writes state file) is seen by a
    fresh process (Gateway) that only reads the persisted file."""
    from antigona.conversation.provider_setup import get_default_provider
    from antigona.providers.resolver import ProviderResolver

    # Simulate a fresh gateway process: NO provider env at all.
    for k in ["ANTIGONA_PROVIDER", "ACTIVE_PROVIDER", "PROVIDER_BASE_URL",
              "OLLAMA_MODEL", "ANTIGONA_MODEL", "ANTIGONA_MODEL_PRIMARY"]:
        monkeypatch.delenv(k, raising=False)

    # Write the persisted state as if another (CLI) process ran /setllm ollama.
    from antigona.providers.resolver import _save_state
    _save_state("ollama", "qwen3.5:4b")

    info = ProviderResolver.get_active_info()
    assert info.status == "active"
    assert info.provider_name == "ollama"
    assert info.model_name == "qwen3.5:4b"
    assert info.base_url == "http://127.0.0.1:11434/v1"

    # get_default_provider (used by /model + DialogueEngine fallback) agrees.
    provider = get_default_provider()
    assert provider is not None
    assert provider._model == "qwen3.5:4b"
    assert "11434" in provider._base_url


def test_split_brain_state_file_beats_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Persisted canonical selection wins over env auto-detection (no split-brain)."""
    from antigona.providers.resolver import ProviderResolver, _save_state

    # Env says deepseek (auto-detect would pick it), but persisted state says ollama.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    _save_state("ollama", "qwen3.5:4b")

    info = ProviderResolver.get_active_info()
    assert info.provider_name == "ollama"
    assert info.model_name == "qwen3.5:4b"


def test_restart_persistence_model(monkeypatch: pytest.MonkeyPatch):
    """After set_active_provider + cache clear (restart), /model still reports the choice."""
    from antigona.providers.resolver import ProviderResolver

    ok, _msg = ProviderResolver.set_active_provider("ollama", "qwen3.5:4b")
    assert ok
    # simulate restart: clear provider cache, drop in-process env override
    ProviderResolver.clear_cache()
    for k in ["ANTIGONA_PROVIDER", "OLLAMA_MODEL", "ANTIGONA_MODEL",
              "ANTIGONA_MODEL_PRIMARY", "PROVIDER_BASE_URL"]:
        monkeypatch.delenv(k, raising=False)

    info = ProviderResolver.get_active_info()
    assert info.status == "active"
    assert info.provider_name == "ollama"
    assert info.model_name == "qwen3.5:4b"


def test_model_endpoint_matches_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """/model and the DialogueEngine resolver share ONE runtime metadata object."""
    from antigona.providers.resolver import ProviderResolver, _save_state

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _save_state("ollama", "qwen3.5:4b")

    info = ProviderResolver.get_active_info()
    provider = ProviderResolver.get_provider()
    assert info.status == "active"
    assert provider is not None
    assert info.model_name == provider._model


def test_mismatched_provider_base_url_does_not_clobber_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state=openrouter + PROVIDER_BASE_URL=deepseek must not mix key/host.

    Regression: OpenRouter key was sent to api.deepseek.com → HTTP 401 →
    dialogue stub «Свободный диалог готов».
    """
    from antigona.conversation.provider_setup import get_default_provider
    from antigona.providers.resolver import ProviderResolver, _save_state

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-not-real")
    monkeypatch.setenv("PROVIDER_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _save_state("openrouter", "openai/gpt-4o-mini")
    ProviderResolver.clear_cache()

    info = ProviderResolver.get_active_info()
    assert info.provider_name == "openrouter"
    assert "openrouter.ai" in info.base_url
    assert "deepseek" not in info.base_url

    provider = get_default_provider()
    assert provider is not None
    assert "openrouter.ai" in provider._base_url
    assert "deepseek" not in provider._base_url
    assert info.base_url.rstrip("/") == provider._base_url.rstrip("/")

