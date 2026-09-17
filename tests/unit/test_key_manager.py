"""Tests for key_manager — parse_key, detect_provider, write_key, verify_key.

Covers:
1. parse_key: extracts sk-or-xxx, sk-xxx, rejects non-keys
2. detect_provider: identifies OpenRouter/SiliconFlow/DeepSeek/generic
3. write_key: creates/updates JSON in secrets dir
4. verify_key: test API request with mock HTTP
5. configure_full_keyflow: end-to-end
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from antigona.tools.key_manager import (
    PROVIDERS,
    configure_full_keyflow,
    detect_provider,
    get_provider_config,
    parse_key,
    verify_key,
    write_key,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_secrets_dir(tmp_path: Path) -> None:
    """Redirect ~/.hermes/secrets to a temp dir for all tests."""
    hermes_dir = Path.home() / ".hermes"
    real_secrets = hermes_dir / "secrets"
    if real_secrets.exists():
        # Don't actually mess with real secrets
        pass

    test_secrets = tmp_path / "hermes_secrets"
    test_secrets.mkdir(parents=True, exist_ok=True)

    with patch.object(Path, "home", return_value=tmp_path):
        yield


# We need a different approach — patch the internal _secrets_dir
@pytest.fixture
def tmp_secrets_dir(tmp_path: Path) -> Path:
    """Create a temp secrets dir and patch _secrets_dir to return it."""
    secrets = tmp_path / ".hermes" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)

    with patch(
        "antigona.tools.key_manager._secrets_dir",
        return_value=secrets,
    ):
        yield secrets


# ─── Tests: parse_key ─────────────────────────────────────────────────────────


class TestParseKey:
    """parse_key extracts API keys from arbitrary text."""

    def test_extracts_openrouter_key(self) -> None:
        text = "Here is my key: sk-" + "or-v1-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-or-")
        assert len(result) > 20

    def test_extracts_deepseek_key(self) -> None:
        text = "Use this DeepSeek key sk-" + "abcdef0123456789abcdef0123456789"
        # DeepSeek keys are 32+ hex chars
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-")

    def test_extracts_siliconflow_key(self) -> None:
        text = "SiliconFlow key: sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-")

    def test_extracts_openai_key(self) -> None:
        text = "OpenAI key is sk-proj-abc123def456ghi789"
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-")

    def test_rejects_empty_string(self) -> None:
        assert parse_key("") is None

    def test_rejects_none(self) -> None:
        assert parse_key(None) is None  # type: ignore[arg-type]

    def test_rejects_text_without_key(self) -> None:
        assert parse_key("Hello, how are you?") is None

    def test_rejects_short_sk(self) -> None:
        assert parse_key("sk-abc") is None

    def test_extracts_key_from_telegram_message(self) -> None:
        text = "вот ключ OpenRouter: sk-or-v1-abc123def456, используй его"
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-or-")

    def test_extracts_key_with_surrounding_text(self) -> None:
        text = """Привет! Вот ключ для OpenRouter: sk-or-v1-abc123def456
Пожалуйста используй его для доступа к API."""
        result = parse_key(text)
        assert result is not None
        assert result.startswith("sk-or-")


# ─── Tests: detect_provider ───────────────────────────────────────────────────


class TestDetectProvider:
    """detect_provider identifies the provider from key prefix."""

    def test_openrouter(self) -> None:
        assert detect_provider("sk-or-v1-abc123def456") == "openrouter"

    def test_deepseek_hex(self) -> None:
        assert detect_provider("sk-" + "abcdef0123456789abcdef0123456789") == "deepseek"

    def test_siliconflow(self) -> None:
        assert detect_provider("sk-" + "abcdefghijklmnopqrstuvwxyz123456") == "siliconflow"

    def test_openai(self) -> None:
        assert detect_provider("sk-proj-abc123def456") == "openai"

    def test_generic_other_format(self) -> None:
        assert detect_provider("some-other-format-key") == "generic"

    def test_empty_string(self) -> None:
        assert detect_provider("") == "generic"

    def test_short_key_fallback(self) -> None:
        assert detect_provider("sk-abcd1234") == "openai"


# ─── Tests: get_provider_config ───────────────────────────────────────────────


class TestGetProviderConfig:
    """get_provider_config returns provider info."""

    def test_known_provider(self) -> None:
        info = get_provider_config("openrouter")
        assert info is not None
        name, env_var, filename, base_url, model = info
        assert name == "OpenRouter"
        assert env_var == "OPENROUTER_API_KEY"
        assert filename == "openrouter.json"
        assert "openrouter.ai" in base_url

    def test_deepseek(self) -> None:
        info = get_provider_config("deepseek")
        assert info is not None
        name, env_var, filename, *_ = info
        assert name == "DeepSeek"
        assert env_var == "DEEPSEEK_API_KEY"

    def test_unknown_provider(self) -> None:
        assert get_provider_config("nonexistent") is None

    def test_all_providers_have_configs(self) -> None:
        for name in PROVIDERS:
            assert get_provider_config(name) is not None


# ─── Tests: write_key ─────────────────────────────────────────────────────────


class TestWriteKey:
    """write_key creates/updates JSON in secrets dir."""

    def test_creates_new_file(self, tmp_secrets_dir: Path) -> None:
        filepath = write_key("openrouter", "sk-or-v1-test-key-12345")
        path = Path(filepath)
        assert path.exists()
        assert path.parent == tmp_secrets_dir

    def test_writes_correct_content(self, tmp_secrets_dir: Path) -> None:
        write_key("openrouter", "sk-or-v1-test-key")
        secrets_file = tmp_secrets_dir / "openrouter.json"
        assert secrets_file.exists()
        data = json.loads(secrets_file.read_text())
        assert data["OPENROUTER_API_KEY"] == "sk-or-v1-test-key"
        assert data["api_key"] == "sk-or-v1-test-key"
        assert "base_url" in data
        assert "updated_at" in data

    def test_writes_deepseek_key(self, tmp_secrets_dir: Path) -> None:
        write_key("deepseek", "sk-" + "abcdef0123456789abcdef0123456789")
        secrets_file = tmp_secrets_dir / "deepseek.json"
        assert secrets_file.exists()
        data = json.loads(secrets_file.read_text())
        assert data["DEEPSEEK_API_KEY"] == "sk-" + "abcdef0123456789abcdef0123456789"
        assert data["api_key"] == "sk-" + "abcdef0123456789abcdef0123456789"

    def test_siliconflow_key(self, tmp_secrets_dir: Path) -> None:
        write_key("siliconflow", "sk-test-siliconflow-key-12345")
        secrets_file = tmp_secrets_dir / "siliconflow.json"
        assert secrets_file.exists()
        data = json.loads(secrets_file.read_text())
        assert data["SILICONFLOW_API_KEY"] == "sk-test-siliconflow-key-12345"

    def test_updates_existing_file(self, tmp_secrets_dir: Path) -> None:
        # Write first
        write_key("openrouter", "sk-or-v1-first-key")
        # Write again with new key
        write_key("openrouter", "sk-or-v1-second-key")
        secrets_file = tmp_secrets_dir / "openrouter.json"
        data = json.loads(secrets_file.read_text())
        assert data["OPENROUTER_API_KEY"] == "sk-or-v1-second-key"
        assert data["api_key"] == "sk-or-v1-second-key"

    def test_raises_on_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            write_key("nonexistent", "sk-test-key")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX st_mode bits (0o600/0o640/0o750) not enforceable on Windows (Wave 4)")
    def test_sets_file_permissions(self, tmp_secrets_dir: Path) -> None:
        write_key("openai", "sk-test-key-67890")
        secrets_file = tmp_secrets_dir / "openai.json"
        # Check mode is 0o600
        assert secrets_file.stat().st_mode & 0o777 == 0o600


# ─── Tests: verify_key ────────────────────────────────────────────────────────


class TestVerifyKey:
    """verify_key makes test API request with mock HTTP."""

    def test_successful_verification(self) -> None:
        with patch("antigona.tools.key_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__enter__.return_value = mock_client

            ok, msg = verify_key("openrouter", "sk-or-v1-test-key")
            assert ok is True
            assert "HTTP 200" in msg

    def test_unauthorized(self) -> None:
        with patch("antigona.tools.key_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__enter__.return_value = mock_client

            ok, msg = verify_key("openrouter", "sk-or-v1-bad-key")
            assert ok is False
            assert "401" in msg

    def test_forbidden(self) -> None:
        with patch("antigona.tools.key_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 403
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__enter__.return_value = mock_client

            ok, msg = verify_key("openrouter", "sk-or-v1-no-perms")
            assert ok is False
            assert "403" in msg

    def test_connection_error(self) -> None:
        with patch(
            "antigona.tools.key_manager.httpx.Client",
            side_effect=Exception("Connection refused"),
        ):
            ok, msg = verify_key("openrouter", "sk-or-v1-test")
            assert ok is False
            assert "error" in msg.lower()

    def test_timeout(self) -> None:
        with patch("antigona.tools.key_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.get.side_effect = __import__("httpx").TimeoutException(
                "timeout", request=None  # type: ignore[arg-type]
            )
            mock_client_cls.return_value.__enter__.return_value = mock_client

            ok, msg = verify_key("openrouter", "sk-or-v1-test")
            assert ok is False
            assert "timed out" in msg.lower()

    def test_unknown_provider(self) -> None:
        ok, msg = verify_key("nonexistent", "sk-test")
        assert ok is False
        assert "Unknown provider" in msg

    def test_verify_deepseek(self) -> None:
        with patch("antigona.tools.key_manager.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value.__enter__.return_value = mock_client

            ok, msg = verify_key("deepseek", "sk-" + "abcdef0123456789abcdef0123456789")
            assert ok is True
            assert "DeepSeek" in msg


# ─── Tests: configure_full_keyflow ────────────────────────────────────────────


class TestConfigureFullKeyflow:
    """configure_full_keyflow runs write → verify → apply."""

    def test_full_success(self, tmp_secrets_dir: Path) -> None:
        with patch("antigona.tools.key_manager.verify_key", return_value=(True, "OK")):
            with patch("antigona.tools.key_manager.apply_provider", return_value=(True, "OK")):
                result = configure_full_keyflow("openrouter", "sk-or-v1-test-key")
                assert result["success"] is True
                assert result["provider"] == "openrouter"
                assert len(result["steps"]) == 3
                # All steps succeeded
                assert all(s["success"] for s in result["steps"])

    def test_verify_fails(self, tmp_secrets_dir: Path) -> None:
        with patch("antigona.tools.key_manager.verify_key", return_value=(False, "Bad key")):
            result = configure_full_keyflow("openrouter", "sk-or-v1-bad-key")
                # write succeeds, verify fails → overall fails
            assert result["success"] is False
            assert result["steps"][0]["success"] is True  # write
            assert result["steps"][1]["success"] is False  # verify
            assert result["steps"][2]["success"] is False  # apply skipped

    def test_unknown_provider(self) -> None:
        result = configure_full_keyflow("nonexistent", "sk-test")
        assert result["success"] is False
        assert "write" in result["steps"][0]["step"]

    def test_key_file_created_and_verified(self, tmp_secrets_dir: Path) -> None:
        """After configure_full_keyflow, the secrets file exists with the key."""
        with patch("antigona.tools.key_manager.verify_key", return_value=(True, "OK")):
            with patch("antigona.tools.key_manager.apply_provider", return_value=(True, "OK")):
                configure_full_keyflow("siliconflow", "sk-test-sf-key")
                secrets_file = tmp_secrets_dir / "siliconflow.json"
                assert secrets_file.exists()
                data = json.loads(secrets_file.read_text())
                assert data["SILICONFLOW_API_KEY"] == "sk-test-sf-key"
