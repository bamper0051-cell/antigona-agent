"""R1-PROVIDER-01 — the Gateway worker's LLM runtime must come from the
canonical ``ProviderResolver`` / persisted ``provider_state.json`` selection,
not from raw legacy ``PROVIDER_BASE_URL`` / ``DEEPSEEK_API_KEY`` /
``OPENROUTER_API_KEY`` env.

Regression target: ``antigona/worker/__init__.py`` built
``build_turn_runtime(base_url=os.getenv("PROVIDER_BASE_URL", "https://api.deepseek.com"),
api_key=os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "",
model=settings.model_primary)`` — a split brain where the operator's `/setllm`
selection was ignored and one provider's key could be sent to another
provider's host (invariants 1, 2, 3, 4, 5).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from antigona.providers.resolver import ProviderResolver, _save_state, _state_file_path

_PROVIDER_ENV_KEYS = (
    "ANTIGONA_PROVIDER",
    "ACTIVE_PROVIDER",
    "PROVIDER_BASE_URL",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTIGONA_MODEL",
    "ANTIGONA_MODEL_PRIMARY",
    "MODEL_PRIMARY",
    "OLLAMA_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_provider_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test gets its own project root so ``provider_state.json`` writes
    never touch the live stack (see T0021 R1-B07)."""
    monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    ProviderResolver.clear_cache()
    yield
    ProviderResolver.clear_cache()


def _worker_llm_config() -> tuple[str, str, str]:
    from antigona.config import Settings
    from antigona.worker import _resolve_worker_llm_config

    return _resolve_worker_llm_config(Settings.from_env())


def test_state_file_isolated_under_tmp() -> None:
    assert str(_state_file_path()).startswith(
        os.environ["ANTIGONA_PROJECT_ROOT"]
    )


def test_worker_llm_config_follows_persisted_openrouter_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/setllm openrouter <model>` + only an OpenRouter key in env →
    worker must call OpenRouter with the OpenRouter key and the persisted
    model. On the buggy code the host was ``api.deepseek.com``."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-not-real")
    _save_state("openrouter", "x-ai/grok-4-fast:free")

    base_url, api_key, model = _worker_llm_config()

    assert "openrouter.ai" in base_url
    assert "deepseek" not in base_url          # invariants 2, 5
    assert api_key == "sk-or-v1-not-real"      # invariant 3 (OpenRouter cred)
    assert model == "x-ai/grok-4-fast:free"    # persisted model, not settings default


def test_worker_never_sends_openrouter_key_to_deepseek_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale ``PROVIDER_BASE_URL=https://api.deepseek.com`` left in the
    environment must not redirect the OpenRouter selection to the DeepSeek
    host carrying the OpenRouter key (invariant 2)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-not-real")
    monkeypatch.setenv("PROVIDER_BASE_URL", "https://api.deepseek.com")
    _save_state("openrouter", "x-ai/grok-4-fast:free")

    base_url, api_key, _model = _worker_llm_config()

    assert "deepseek" not in base_url
    assert not (api_key == "sk-or-v1-not-real" and "deepseek" in base_url)


def test_worker_llm_config_follows_persisted_deepseek_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive case: DeepSeek selected with a DeepSeek key → DeepSeek host,
    DeepSeek key, DeepSeek profile default model. No cross-provider bleed."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-not-real")
    _save_state("deepseek", "deepseek-v4-flash")

    base_url, api_key, model = _worker_llm_config()

    assert "deepseek" in base_url
    assert "openrouter" not in base_url
    assert api_key == "sk-ds-not-real"
    assert model == "deepseek-v4-flash"


def test_worker_llm_config_matches_get_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker triple must be exactly what the canonical
    ``get_default_provider()`` resolves — one authority, no second code path."""
    from antigona.conversation.provider_setup import get_default_provider

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-not-real")
    _save_state("openrouter", "x-ai/grok-4-fast:free")

    base_url, api_key, model = _worker_llm_config()
    canon = get_default_provider()
    assert canon is not None
    assert base_url.rstrip("/") == canon._base_url.rstrip("/")
    assert api_key == canon._api_key
    assert model == canon._model
