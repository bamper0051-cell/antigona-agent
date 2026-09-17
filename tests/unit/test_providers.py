"""DAY 12 — Tests for the Provider abstraction layer.

Tests:
  1. BaseProvider is abstract and cannot be instantiated
  2. MockProvider returns pre-configured responses in order
  3. MockProvider cycles through responses
  4. MockProvider reset works
  5. OpenAICompatibleProvider — successful request via mock httpx
  6. OpenAICompatibleProvider — HTTP error handling
  7. OpenAICompatibleProvider — API error response handling
  8. ProviderRegistry — register, select, and list
  9. ProviderRegistry — generate delegates to active provider
  10. ProviderRegistry — unregister and auto-fallback
  11. ProviderRegistry — error on missing provider
  12. ConversationEngine — works without network via MockProvider
  13. ConversationEngine — falls back to provider for unknown inputs
  14. ConversationEngine — still works without any provider (classic mode)
  15. ConversationEngine — provider failure returns graceful fallback
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from antigona.conversation.engine import ConversationEngine, chitchat_reply
from antigona.providers.base import BaseProvider
from antigona.providers.mock import MockProvider
from antigona.providers.openai_compatible import OpenAICompatibleProvider
from antigona.providers.registry import ProviderRegistry, ProviderRegistryError

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: BaseProvider is abstract
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseProvider:
    """BaseProvider must be abstract and enforce the interface."""

    def test_cannot_instantiate_directly(self) -> None:
        """BaseProvider cannot be instantiated — it's abstract."""
        with pytest.raises(TypeError, match="abstract"):
            BaseProvider()  # type: ignore[abstract]

    def test_subclass_without_generate_raises(self) -> None:
        """A subclass without generate() raises TypeError."""
        with pytest.raises(TypeError, match="abstract"):

            class Incomplete(BaseProvider):  # type: ignore[misc]
                name = "incomplete"

            Incomplete()


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: MockProvider
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockProvider:
    """MockProvider tests — pre-configured responses, cycling, reset."""

    def test_default_responses(self) -> None:
        """Default MockProvider returns predefined responses."""
        provider = MockProvider()
        reply = provider.generate([{"role": "user", "content": "hello"}])
        assert "MockProvider" in reply
        assert provider.call_count == 1

    def test_custom_responses(self) -> None:
        """MockProvider with custom responses."""
        responses = ["First", "Second", "Third"]
        provider = MockProvider(responses=responses)
        assert provider.generate([]) == "First"
        assert provider.generate([]) == "Second"
        assert provider.generate([]) == "Third"

    def test_cycles_through_responses(self) -> None:
        """MockProvider cycles after exhausting the list."""
        provider = MockProvider(responses=["A", "B"])
        assert provider.generate([]) == "A"
        assert provider.generate([]) == "B"
        assert provider.generate([]) == "A"  # cycles
        assert provider.generate([]) == "B"  # cycles

    def test_reset(self) -> None:
        """reset() resets the call counter."""
        provider = MockProvider()
        provider.generate([])
        provider.generate([])
        assert provider.call_count == 2
        provider.reset()
        assert provider.call_count == 0
        # After reset starts from first response again
        assert "MockProvider" in provider.generate([])

    def test_empty_responses(self) -> None:
        """Empty response list returns empty string."""
        provider = MockProvider(responses=[])
        assert provider.generate([]) == ""

    def test_single_response(self) -> None:
        """Single response is returned on every call."""
        provider = MockProvider(responses=["Always this"])
        assert provider.generate([]) == "Always this"
        assert provider.generate([]) == "Always this"

    def test_generate_with_context(self) -> None:
        """generate accepts optional context (ignored by MockProvider)."""
        provider = MockProvider()
        reply = provider.generate(
            [{"role": "user", "content": "test"}],
            context={"temperature": 0.5},
        )
        assert isinstance(reply, str)
        assert len(reply) > 0

    def test_name_is_mock(self) -> None:
        """MockProvider.name must be 'mock'."""
        provider = MockProvider()
        assert provider.name == "mock"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: OpenAICompatibleProvider
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenAICompatibleProvider:
    """OpenAICompatibleProvider tests with mocked HTTP."""

    def _make_mock_response(
        self, content: str = "Hello from API!", status_code: int = 200
    ) -> MagicMock:
        """Build a mock httpx.Response."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = status_code
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        return mock_resp

    def test_successful_request(self) -> None:
        """Successful API call returns the response content."""
        provider = OpenAICompatibleProvider(
            base_url="https://test-api.example.com/v1",
            api_key="test-key",
            model="test-model",
        )
        mock_resp = self._make_mock_response("Hello from API!")

        with patch.object(httpx.Client, "post", return_value=mock_resp) as mock_post:
            reply = provider.generate([{"role": "user", "content": "hi"}])

            assert reply == "Hello from API!"
            mock_post.assert_called_once()
            # Verify correct endpoint was called
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"
            assert call_kwargs["json"]["model"] == "test-model"

    def test_context_overrides_model_and_temp(self) -> None:
        """Context overrides default model and temperature."""
        provider = OpenAICompatibleProvider(api_key="key", model="default-model")
        mock_resp = self._make_mock_response("Context override")

        with patch.object(httpx.Client, "post", return_value=mock_resp) as mock_post:
            provider.generate(
                [{"role": "user", "content": "hi"}],
                context={"model": "custom-model", "temperature": 0.2},
            )

            call_json = mock_post.call_args[1]["json"]
            assert call_json["model"] == "custom-model"
            assert call_json["temperature"] == 0.2

    def test_http_error(self) -> None:
        """HTTP error raises ProviderError."""
        provider = OpenAICompatibleProvider(api_key="key")
        mock_resp = self._make_mock_response("Not found", status_code=404)

        with patch.object(httpx.Client, "post", return_value=mock_resp):
            with pytest.raises(Exception, match="404"):
                provider.generate([{"role": "user", "content": "hi"}])

    def test_http_request_error(self) -> None:
        """Network error raises ProviderError."""
        provider = OpenAICompatibleProvider(api_key="key")

        with patch.object(httpx.Client, "post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(Exception, match="HTTP request failed"):
                provider.generate([{"role": "user", "content": "hi"}])

    def test_api_error_response(self) -> None:
        """Malformed API response raises ProviderError."""
        provider = OpenAICompatibleProvider(api_key="key")
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"invalid": "response"}

        with patch.object(httpx.Client, "post", return_value=mock_resp):
            with pytest.raises(Exception, match="Unexpected API response"):
                provider.generate([{"role": "user", "content": "hi"}])

    def test_close_cleans_up(self) -> None:
        """close() cleans up the HTTP client."""
        provider = OpenAICompatibleProvider(api_key="key")
        # Trigger lazy init
        _ = provider._http_client  # noqa: SLF001
        assert provider._client is not None  # noqa: SLF001
        provider.close()
        assert provider._client is None  # noqa: SLF001

    def test_name(self) -> None:
        """OpenAICompatibleProvider.name must be 'openai_compatible'."""
        provider = OpenAICompatibleProvider(api_key="key")
        assert provider.name == "openai_compatible"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: ProviderRegistry
# ═══════════════════════════════════════════════════════════════════════════════


class TestProviderRegistry:
    """ProviderRegistry tests — registration, selection, delegation."""

    def test_register_and_list(self) -> None:
        """Register providers and list them."""
        registry = ProviderRegistry()
        registry.register(MockProvider(), name="mock")
        registry.register(MockProvider(responses=["custom"]), name="custom")

        providers = registry.list_providers()
        assert "mock" in providers
        assert "custom" in providers
        assert providers["mock"] == "MockProvider"

    def test_auto_select_first(self) -> None:
        """First registered provider is auto-selected."""
        registry = ProviderRegistry()
        registry.register(MockProvider(responses=["auto"]), name="auto")
        assert registry.active_name == "auto"
        assert registry.active is not None

    def test_select_and_generate(self) -> None:
        """select() switches active provider and generate() delegates."""
        registry = ProviderRegistry()
        m1 = MockProvider(responses=["from A"])
        m2 = MockProvider(responses=["from B"])
        registry.register(m1, name="A")
        registry.register(m2, name="B")

        registry.select("B")
        assert registry.active_name == "B"
        reply = registry.generate([{"role": "user", "content": "hi"}])
        assert reply == "from B"

        registry.select("A")
        reply = registry.generate([{"role": "user", "content": "hi"}])
        assert reply == "from A"

    def test_unregister_removes_provider(self) -> None:
        """Unregister removes a provider from the registry."""
        registry = ProviderRegistry()
        registry.register(MockProvider(), name="mock")
        assert "mock" in registry.list_providers()
        registry.unregister("mock")
        assert "mock" not in registry.list_providers()

    def test_unregister_switches_active(self) -> None:
        """Unregistering the active provider switches to another."""
        registry = ProviderRegistry()
        registry.register(MockProvider(responses=["first"]), name="first")
        registry.register(MockProvider(responses=["second"]), name="second")
        registry.select("first")
        registry.unregister("first")
        assert registry.active_name == "second"

    def test_unregister_last_leaves_no_active(self) -> None:
        """Unregistering the last provider leaves no active provider."""
        registry = ProviderRegistry()
        registry.register(MockProvider(), name="only")
        registry.unregister("only")
        assert registry.active_name is None
        assert registry.active is None

    def test_unregister_unknown_raises(self) -> None:
        """Unregistering unknown name raises ProviderRegistryError."""
        registry = ProviderRegistry()
        with pytest.raises(ProviderRegistryError, match="not registered"):
            registry.unregister("ghost")

    def test_select_unknown_raises(self) -> None:
        """Selecting unknown name raises ProviderRegistryError."""
        registry = ProviderRegistry()
        with pytest.raises(ProviderRegistryError, match="not found"):
            registry.select("ghost")

    def test_generate_without_provider_raises(self) -> None:
        """generate() with no registered provider raises."""
        registry = ProviderRegistry()
        with pytest.raises(ProviderRegistryError, match="No provider"):
            registry.generate([{"role": "user", "content": "hi"}])

    def test_register_uses_provider_name_when_no_alias(self) -> None:
        """register() without explicit name uses provider.name."""
        registry = ProviderRegistry()
        provider = MockProvider()
        key = registry.register(provider)
        assert key == "mock"
        assert registry.active_name == "mock"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: ConversationEngine with Provider (network-free)
# ═══════════════════════════════════════════════════════════════════════════════


class TestConversationEngineWithProvider:
    """ConversationEngine works without network via MockProvider."""

    def test_classic_mode_without_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConversationEngine without any provider raises (no rule-based mode)."""
        monkeypatch.setattr(
            "antigona.providers.resolver.ProviderResolver.get_provider", lambda: None
        )
        engine = ConversationEngine()
        with pytest.raises(ValueError, match="No active LLM provider"):
            engine.reply("Привет")

    def test_with_mock_provider_fallback(self) -> None:
        """Every message goes through the configured provider (pure-LLM)."""
        mock = MockProvider(responses=["Offline ответ от MockProvider."])
        engine = ConversationEngine(provider=mock)

        # All messages (greeting or not) go to the provider — no rule-based echo.
        reply = engine.reply("Привет")
        assert "Offline" in reply
        assert mock.call_count == 1

        reply2 = engine.reply("расскажи анекдот")
        assert "Offline" in reply2
        assert mock.call_count == 2

    def test_network_free_operation(self) -> None:
        """Full conversation without network via MockProvider (pure-LLM)."""
        mock = MockProvider(responses=["Ответ 1", "Ответ 2", "Ответ 3"])
        engine = ConversationEngine(provider=mock)

        # Greeting → provider call (no rule-based canned greeting).
        assert "Ответ 1" in engine.reply("Привет")
        assert mock.call_count == 1

        assert "Ответ 2" in engine.reply("what is the meaning of life")
        assert mock.call_count == 2

        # Thanks → provider call (no canned "Пожалуйста").
        assert "Ответ 3" in engine.reply("спасибо")
        assert mock.call_count == 3

        # Cycles after responses exhausted.
        assert "Ответ 1" in engine.reply("расскажи про offline режим")
        assert mock.call_count == 4

    def test_provider_failure_graceful(self) -> None:
        """A provider exception propagates to the caller (no silent masking)."""
        class BrokenProvider(BaseProvider):  # type: ignore[misc]
            name = "broken"

            def generate(  # type: ignore[override]
                self, messages: list[dict[str, str]], context: dict[str, Any] | None = None
            ) -> str:
                raise RuntimeError("Broken!")

        engine = ConversationEngine(provider=BrokenProvider())
        with pytest.raises(RuntimeError, match="Broken!"):
            engine.reply("some random question")

    def test_context_passed_through(self) -> None:
        """Context dict is stored as last_context."""
        engine = ConversationEngine(provider=MockProvider())
        engine.reply("Привет", context={"correlation_id": "abc-123"})
        assert engine.last_context.get("correlation_id") == "abc-123"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: End-to-end — ProviderRegistry + ConversationEngine
# ═══════════════════════════════════════════════════════════════════════════════


class TestProviderIntegration:
    """End-to-end integration: registry + conversation engine."""

    def test_registry_generate_delegates_to_mock(self) -> None:
        """ProviderRegistry.generate() delegates to the active MockProvider."""
        registry = ProviderRegistry()
        registry.register(
            MockProvider(responses=["Offline ответ без Gateway."]),
            name="offline",
        )
        reply = registry.generate([{"role": "user", "content": "что-то сложное"}])
        assert "Offline" in reply
        assert registry.active_name == "offline"

    def test_registry_switch_and_generate(self) -> None:
        """Switching provider in registry changes generate() output."""
        registry = ProviderRegistry()
        registry.register(MockProvider(responses=["from A"]), name="A")
        registry.register(MockProvider(responses=["from B"]), name="B")
        registry.select("A")
        assert "from A" in registry.generate([{"role": "user", "content": "hi"}])

        registry.select("B")
        assert "from B" in registry.generate([{"role": "user", "content": "hi"}])

    def test_chitchat_reply_function_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Standalone chitchat_reply() forwards to the configured provider."""
        mock = MockProvider(responses=["Привет! Чем помочь?"])
        reply = chitchat_reply("Привет", provider=mock)
        assert "Привет" in reply

        mock2 = MockProvider(responses=["Всегда пожалуйста! Обращайся 😊"])
        reply2 = chitchat_reply("Спасибо", provider=mock2)
        assert "пожалуйста" in reply2.lower()

        # Without any provider, chitchat_reply raises (no rule-based mode).
        monkeypatch.setattr(
            "antigona.providers.resolver.ProviderResolver.get_provider", lambda: None
        )
        with pytest.raises(ValueError, match="No active LLM provider"):
            chitchat_reply("Привет")
