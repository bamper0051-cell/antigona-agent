"""P1-03 (Antigona R2 Codex review): ProviderResolver fail-open cross-provider fallback.

Canonical finding — evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md:

    [P1] src/antigona/conversation/provider_setup.py:56-94,108-129;
         src/antigona/worker/__init__.py:297-307;
         src/antigona/worker/quarantine.py:171-184 —
    При persisted selection openrouter, отсутствующем OpenRouter key и
    присутствующем DeepSeek key explicit-ветка проваливается в auto-detection
    и возвращает DeepSeek — primary prompts и quarantine content отправляются
    не выбранному владельцем провайдеру вместо fail-closed отказа —
    тесты либо задают key выбранного provider, либо удаляют все credentials;
    mixed-key failure не покрыт — blocking

Requirements:
- Explicit provider selection (state file or env) must FAIL-CLOSED when its
  credentials/profile are missing or unavailable.
- Under NO circumstances may an explicit selection fall through to auto-detection
  or another provider.
- Auto mode (auto or empty) continues to perform allowed auto-detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.conversation.provider_setup import get_default_provider
from antigona.providers.resolver import ProviderResolver, _save_state
from antigona.worker import _resolve_worker_llm_config
from antigona.worker.quarantine import QuarantineModel, QuarantineUnavailableError

_ENV_KEYS = (
    "ANTIGONA_PROVIDER",
    "ACTIVE_PROVIDER",
    "PROVIDER_BASE_URL",
    "SILICONFLOW_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTIGONA_OPENROUTER_API_KEY",
    "ANTIGONA_OPENROUTER_BASE_URL",
    "ANTIGONA_MODEL",
    "ANTIGONA_MODEL_PRIMARY",
    "MODEL_PRIMARY",
    "OLLAMA_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    ProviderResolver.clear_cache()
    yield
    ProviderResolver.clear_cache()


def test_explicit_openrouter_without_key_fails_closed_in_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit OpenRouter selected, but OPENROUTER_API_KEY is missing while
    DEEPSEEK_API_KEY is present in env.

    Worker startup must fail-closed (raise RuntimeError) and NOT silently resolve
    DeepSeek host/key.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-present")
    _save_state("openrouter", "openai/gpt-4o-mini")

    assert get_default_provider() is None

    info = ProviderResolver.get_active_info()
    assert info.status == "unresolved"
    assert ProviderResolver.get_provider() is None

    with pytest.raises(RuntimeError, match="no LLM provider resolved"):
        _resolve_worker_llm_config(Settings.from_env())


def test_explicit_openrouter_without_key_fails_closed_in_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit OpenRouter selected without key, DeepSeek key present.

    Quarantine must not resolve to DeepSeek transport.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-present")
    _save_state("openrouter", "openai/gpt-4o-mini")

    qm = QuarantineModel(primary_model="primary-m", quarantine_model="quarantine-m")
    assert getattr(qm.provider, "api_key", None) == ""
    assert getattr(qm.provider, "api_url", None) == ""

    with pytest.raises(QuarantineUnavailableError):
        qm.sanitize("untrusted input")


def test_explicit_deepseek_without_key_fails_closed_despite_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit DeepSeek selected, but DEEPSEEK_API_KEY is missing while
    OPENROUTER_API_KEY is present.

    Must fail-closed and not fall back to OpenRouter.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-present")
    _save_state("deepseek", "deepseek-chat")

    assert get_default_provider() is None
    assert ProviderResolver.get_active_info().status == "unresolved"
    assert ProviderResolver.get_provider() is None

    with pytest.raises(RuntimeError, match="no LLM provider resolved"):
        _resolve_worker_llm_config(Settings.from_env())


def test_explicit_unknown_provider_fails_closed_despite_available_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit selection of a nonexistent/unknown provider must fail closed."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-present")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-present")
    _save_state("custom_unknown_provider", "some-model")

    assert get_default_provider() is None
    assert ProviderResolver.get_active_info().status == "unresolved"
    assert ProviderResolver.get_provider() is None


def test_auto_mode_still_detects_available_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode (no explicit provider or provider='auto') continues to auto-detect."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-auto")
    _save_state("auto", "")

    provider = get_default_provider()
    assert provider is not None
    assert "deepseek" in provider._base_url
    assert provider._api_key == "sk-deepseek-auto"

    info = ProviderResolver.get_active_info()
    assert info.status == "active"
    assert info.provider_name == "deepseek"


def test_explicit_provider_with_valid_key_resolves_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the explicitly selected provider HAS its key, it resolves normally
    even if another provider key is also present."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-valid")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-also-present")
    _save_state("openrouter", "openai/gpt-4o-mini")

    provider = get_default_provider()
    assert provider is not None
    assert "openrouter.ai" in provider._base_url
    assert "deepseek" not in provider._base_url
    assert provider._api_key == "sk-openrouter-valid"

    base_url, api_key, model = _resolve_worker_llm_config(Settings.from_env())
    assert "openrouter.ai" in base_url
    assert api_key == "sk-openrouter-valid"
    assert model == "openai/gpt-4o-mini"


def test_explicit_siliconflow_uses_public_endpoint_and_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit SiliconFlow selection preserves its public endpoint and model."""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "«redacted:sk-…»")
    monkeypatch.setenv("PROVIDER_BASE_URL", "https://api.siliconflow.com/v1/")
    monkeypatch.setenv("ANTIGONA_MODEL", "Qwen/Qwen2.5-72B")
    _save_state("siliconflow", "")

    provider = get_default_provider()
    assert provider is not None
    assert provider._base_url == "https://api.siliconflow.com/v1"
    assert provider._model == "Qwen/Qwen2.5-72B"

    info = ProviderResolver.get_active_info()
    assert info.status == "active"
    assert info.provider_name == "siliconflow"
    assert info.base_url == "https://api.siliconflow.com/v1"
    assert info.model_name == "Qwen/Qwen2.5-72B"
