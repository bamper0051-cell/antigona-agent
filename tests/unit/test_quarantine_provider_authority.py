"""R1-PROVIDER-02 — the quarantine sanitiser's provider must be resolved from
the canonical ``ProviderResolver`` / persisted ``provider_state.json``
selection, not hardcoded to ``OPENROUTER_*`` env with an OpenRouter URL
default.

Regression target: ``antigona/worker/quarantine.py`` ``QuarantineModel.__init__``
built ``HTTPQuarantineProvider`` from
``os.getenv("ANTIGONA_OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")``
+ ``os.getenv("ANTIGONA_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")``
— so enabling quarantine could call a different provider than the active
selection, with legacy env vars silently choosing host and credential
(invariants 3, 4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.providers.resolver import ProviderResolver, _save_state
from antigona.worker.quarantine import HTTPQuarantineProvider, QuarantineModel

_ENV_KEYS = (
    "ANTIGONA_PROVIDER",
    "ACTIVE_PROVIDER",
    "PROVIDER_BASE_URL",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTIGONA_OPENROUTER_API_KEY",
    "ANTIGONA_OPENROUTER_BASE_URL",
    "ANTIGONA_MODEL",
    "ANTIGONA_MODEL_PRIMARY",
    "MODEL_PRIMARY",
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


def test_quarantine_provider_follows_canonical_deepseek_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator selected DeepSeek → the quarantine HTTP provider must target
    the DeepSeek host with the DeepSeek credential, not OpenRouter."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-not-real")
    _save_state("deepseek", "deepseek-chat")

    qm = QuarantineModel(
        primary_model="deepseek-v4-flash", quarantine_model="deepseek-chat"
    )
    prov = qm.provider
    assert isinstance(prov, HTTPQuarantineProvider)
    assert "deepseek" in prov.api_url
    assert "openrouter" not in prov.api_url
    assert prov.api_url.endswith("/chat/completions")
    assert prov.api_key == "sk-ds-not-real"


def test_quarantine_provider_ignores_legacy_openrouter_env_when_deepseek_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale ``OPENROUTER_API_KEY`` / ``ANTIGONA_OPENROUTER_BASE_URL`` in the
    environment must not override the DeepSeek selection (invariant 4)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-not-real")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-not-real")
    monkeypatch.setenv(
        "ANTIGONA_OPENROUTER_BASE_URL",
        "https://openrouter.ai/api/v1/chat/completions",
    )
    _save_state("deepseek", "deepseek-chat")

    prov = QuarantineModel(
        primary_model="deepseek-v4-flash", quarantine_model="deepseek-chat"
    ).provider
    assert isinstance(prov, HTTPQuarantineProvider)
    assert "openrouter" not in prov.api_url
    assert prov.api_key == "sk-ds-not-real"


def test_quarantine_provider_follows_canonical_openrouter_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Common case unchanged: OpenRouter selected + OpenRouter key → OpenRouter
    host with the OpenRouter credential."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-not-real")
    _save_state("openrouter", "x-ai/grok-4-fast:free")

    prov = QuarantineModel(
        primary_model="deepseek-v4-flash",
        quarantine_model="x-ai/grok-4-fast:free",
    ).provider
    assert isinstance(prov, HTTPQuarantineProvider)
    assert "openrouter.ai" in prov.api_url
    assert prov.api_url.endswith("/chat/completions")
    assert prov.api_key == "sk-or-not-real"


def test_quarantine_still_fails_closed_without_any_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider state, no keys → canonical resolver yields the local Ollama
    fallback (no credential) → quarantine is unavailable, never a silent
    remote call."""
    from antigona.worker.quarantine import QuarantineUnavailableError

    qm = QuarantineModel(
        primary_model="primary-m", quarantine_model="quarantine-m"
    )
    with pytest.raises(QuarantineUnavailableError):
        qm.sanitize("some untrusted text")
