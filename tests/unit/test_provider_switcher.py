"""Tests for provider_switcher — switch, list, test providers.

Covers:
1. switch_to_provider: switching to a known provider
2. get_available_providers: listing providers with status
3. test_current_provider: testing the active provider
4. format_provider_list: display formatting
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from antigona.tools.provider_switcher import (
    format_provider_list,
    get_available_providers,
    switch_to_provider,
)
from antigona.tools.provider_switcher import (
    test_current_provider as _test_current_provider,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_secrets_dir(tmp_path: Path) -> Path:
    """Create a temp secrets dir with test provider files."""
    secrets = tmp_path / ".hermes" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)

    # Create test files
    openrouter_data = {
        "OPENROUTER_API_KEY": "sk-or-v1-test-key-12345",
        "api_key": "sk-or-v1-test-key-12345",
        "base_url": "https://openrouter.ai/api/v1",
    }
    deepseek_data = {
        "DEEPSEEK_API_KEY": "sk-" + "abcdef0123456789abcdef0123456789",
        "api_key": "sk-" + "abcdef0123456789abcdef0123456789",
        "base_url": "https://api.deepseek.com",
    }
    (secrets / "openrouter.json").write_text(json.dumps(openrouter_data))
    (secrets / "deepseek.json").write_text(json.dumps(deepseek_data))

    with patch("antigona.tools.provider_switcher._secrets_dir", return_value=secrets):
        yield secrets


# ─── Tests: switch_to_provider ────────────────────────────────────────────────


class TestSwitchToProvider:
    """switch_to_provider switches to a configured provider."""

    def test_switch_to_known_provider(self, tmp_secrets_dir: Path) -> None:
        mock_provider = MagicMock()
        mock_provider.generate.return_value = "OK"
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = switch_to_provider("openrouter")
            assert ok is True
            assert "OpenRouter" in msg

    def test_switch_to_deepseek(self, tmp_secrets_dir: Path) -> None:
        mock_provider = MagicMock()
        mock_provider.generate.return_value = "OK"
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = switch_to_provider("deepseek")
            assert ok is True
            assert "DeepSeek" in msg

    def test_unknown_provider(self) -> None:
        ok, msg = switch_to_provider("nonexistent")
        assert ok is False
        assert "Unknown provider" in msg

    def test_no_secrets_file(self, tmp_path: Path) -> None:
        """Provider without secrets file."""
        empty_secrets = tmp_path / ".hermes" / "secrets"
        empty_secrets.mkdir(parents=True, exist_ok=True)
        with patch("antigona.tools.provider_switcher._secrets_dir", return_value=empty_secrets):
            ok, msg = switch_to_provider("openrouter")
            assert ok is False
            assert "No secrets file" in msg

    def test_empty_secrets_file(self, tmp_secrets_dir: Path) -> None:
        """Provider with an empty secrets file (no api_key)."""
        # Write an empty JSON
        (tmp_secrets_dir / "siliconflow.json").write_text(json.dumps({"note": "empty"}))
        ok, msg = switch_to_provider("siliconflow")
        assert ok is False
        assert "No API key found" in msg

    def test_verification_failure(self, tmp_secrets_dir: Path) -> None:
        """Provider that fails the test call."""
        mock_provider = MagicMock()
        mock_provider.generate.side_effect = RuntimeError("API unavailable")
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = switch_to_provider("openrouter")
            assert ok is False
            assert "test call failed" in msg

    def test_normalizes_name(self, tmp_secrets_dir: Path) -> None:
        mock_provider = MagicMock()
        mock_provider.generate.return_value = "OK"
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = switch_to_provider("OpenRouter")
            assert ok is True
            assert "OpenRouter" in msg


# ─── Tests: get_available_providers ───────────────────────────────────────────


class TestGetAvailableProviders:
    """get_available_providers lists all providers with status."""

    def test_returns_all_providers(self, tmp_secrets_dir: Path) -> None:
        providers = get_available_providers()
        names = [p["name"] for p in providers]
        assert "openrouter" in names
        assert "deepseek" in names
        assert "siliconflow" in names

    def test_openrouter_has_secrets(self, tmp_secrets_dir: Path) -> None:
        providers = get_available_providers()
        or_provider = next(p for p in providers if p["name"] == "openrouter")
        assert or_provider["has_secrets"] is True
        assert or_provider["has_key"] is True

    def test_siliconflow_no_secrets(self, tmp_secrets_dir: Path) -> None:
        """SiliconFlow doesn't have a file created in our fixture."""
        providers = get_available_providers()
        sf_provider = next(p for p in providers if p["name"] == "siliconflow")
        assert sf_provider["has_secrets"] is False
        assert sf_provider["has_key"] is False

    def test_provider_display_name(self, tmp_secrets_dir: Path) -> None:
        providers = get_available_providers()
        or_provider = next(p for p in providers if p["name"] == "openrouter")
        assert or_provider["display_name"] == "OpenRouter"

    def test_provider_model(self, tmp_secrets_dir: Path) -> None:
        providers = get_available_providers()
        or_provider = next(p for p in providers if p["name"] == "openrouter")
        assert or_provider["model"] is not None

    def test_no_providers_configured(self, tmp_path: Path) -> None:
        """Empty secrets dir."""
        empty_secrets = tmp_path / ".hermes" / "secrets"
        empty_secrets.mkdir(parents=True, exist_ok=True)
        with patch("antigona.tools.provider_switcher._secrets_dir", return_value=empty_secrets):
            providers = get_available_providers()
            assert len(providers) == len(
                __import__("antigona.tools.key_manager", fromlist=["PROVIDERS"]).PROVIDERS
            )
            # All should have has_secrets=False
            assert all(p["has_secrets"] is False for p in providers)


# ─── Tests: test_current_provider ─────────────────────────────────────────────


class TestTestCurrentProvider:
    """test_current_provider tests the active provider."""

    def test_no_provider(self) -> None:
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=None,
        ):
            ok, msg = _test_current_provider()
            assert ok is False
            assert "No active LLM provider" in msg

    def test_provider_responds(self) -> None:
        mock_provider = MagicMock()
        mock_provider.generate.return_value = "OK"
        mock_provider._base_url = "https://api.test.com/v1"
        mock_provider._model = "test-model"
        mock_provider._api_key = "sk-test-key"

        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = _test_current_provider()
            assert ok is True
            assert "Provider OK" in msg

    def test_provider_fails(self) -> None:
        mock_provider = MagicMock()
        mock_provider.generate.side_effect = RuntimeError("API down")
        mock_provider._base_url = "https://api.test.com/v1"
        mock_provider._model = "test-model"
        mock_provider._api_key = "sk-test-key"

        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = _test_current_provider()
            assert ok is False
            assert "test failed" in msg

    def test_non_openai_provider(self) -> None:
        """Provider without _api_key attribute."""
        mock_provider = MagicMock(spec=["generate"])
        mock_provider.name = "other"
        mock_provider._base_url = "https://api.test.com/v1"
        mock_provider._model = "test-model"
        mock_provider._api_key = "sk-test-key"
        mock_provider.generate.return_value = "OK"

        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = _test_current_provider()
            assert ok is True

    def test_no_api_key(self) -> None:
        mock_provider = MagicMock()
        mock_provider._base_url = "https://api.test.com/v1"
        mock_provider._model = "test-model"
        mock_provider._api_key = ""

        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider",
            return_value=mock_provider,
        ):
            ok, msg = _test_current_provider()
            assert ok is False
            assert "api key" in msg.lower()


# ─── Tests: format_provider_list ──────────────────────────────────────────────


class TestFormatProviderList:
    """format_provider_list produces user-readable output."""

    def test_returns_string(self) -> None:
        providers = [
            {
                "name": "openrouter",
                "display_name": "OpenRouter",
                "has_secrets": True,
                "has_key": True,
                "env_set": False,
                "is_active": True,
                "model": "openai/gpt-4o-mini",
                "base_url": "https://openrouter.ai/api/v1",
                "status": "✅ active",
            },
            {
                "name": "deepseek",
                "display_name": "DeepSeek",
                "has_secrets": True,
                "has_key": True,
                "env_set": False,
                "is_active": False,
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com",
                "status": "💾 stored",
            },
        ]
        result = format_provider_list(providers)
        assert isinstance(result, str)
        assert "OpenRouter" in result
        assert "DeepSeek" in result
        assert "provider list" in result.lower()

    def test_empty_list(self) -> None:
        result = format_provider_list([])
        assert isinstance(result, str)
        assert "Доступные провайдеры" in result

    def test_includes_commands(self) -> None:
        providers = [{
            "name": "openrouter",
            "display_name": "OpenRouter",
            "has_secrets": True,
            "has_key": True,
            "env_set": False,
            "is_active": True,
            "model": "openai/gpt-4o-mini",
            "base_url": "https://openrouter.ai/api/v1",
            "status": "✅ active",
        }]
        result = format_provider_list(providers)
        assert "/provider list" in result
        assert "/provider switch" in result
        assert "/provider test" in result
