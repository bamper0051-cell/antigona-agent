"""Tests for ConversationEngine LLM provider integration.

Covers:
1. reply_with_provider with a mock provider (happy path)
2. reply_with_provider with provider=None raises ValueError
3. reply_with_provider when provider.generate() raises (propagates)
4. reply_with_provider with context (turn_buffer, session_summary)
5. chitchat_reply: no provider -> uses get_default_provider
6. chitchat_reply: with provider -> LLM response
7. chitchat_reply: no key available -> RuntimeError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from antigona.conversation.engine import (
    ConversationEngine,
    chitchat_reply,
)
from antigona.providers.base import BaseProvider

# --- Mock provider ------------------------------------------------------------


class MockProvider(BaseProvider):
    """A provider that returns a canned response for testing."""

    name: str = "mock"

    def __init__(self, response: str = "Mock response", fail: bool = False) -> None:
        self._response = response
        self._fail = fail
        self.last_messages: list[dict[str, str]] | None = None
        self.last_context: dict[str, Any] | None = None

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        self.last_messages = messages
        self.last_context = context
        if self._fail:
            msg = self._response or "Mock failure"
            raise RuntimeError(msg)
        return self._response


class TrackingProvider(BaseProvider):
    """A provider that records how many times generate() was called."""

    name: str = "tracking"

    def __init__(self, response: str = "LLM reply") -> None:
        self._response = response
        self.call_count = 0

    def generate(
        self,
        messages: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> str:
        self.call_count += 1
        return self._response


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def engine() -> ConversationEngine:
    return ConversationEngine()


@pytest.fixture
def engine_with_mock(mock_provider: MockProvider) -> ConversationEngine:
    return ConversationEngine(provider=mock_provider)


# --- Tests: reply_with_provider ------------------------------------------------


class TestReplyWithProvider:
    """Tests for the public reply_with_provider method."""

    def test_happy_path(self, engine: ConversationEngine, mock_provider: MockProvider) -> None:
        """Provider is called and its output is returned."""
        response = engine.reply_with_provider("How are you?", provider=mock_provider)
        assert response == "Mock response"
        assert mock_provider.last_messages is not None
        roles = [m["role"] for m in mock_provider.last_messages]
        assert "system" in roles
        assert mock_provider.last_messages[-1] == {"role": "user", "content": "How are you?"}

    def test_provider_none_raises(self, engine: ConversationEngine) -> None:
        """When provider is None, raise ValueError - no static fallback."""
        with pytest.raises(ValueError, match="provider is required"):
            engine.reply_with_provider("How are you?", provider=None)

    def test_provider_raises_exception(self, engine: ConversationEngine) -> None:
        """When provider.generate() raises, the exception propagates."""
        failing = MockProvider(fail=True)
        with pytest.raises(RuntimeError, match="Mock response"):
            engine.reply_with_provider("Something complex", provider=failing)

    def test_with_turn_buffer_context(self, engine: ConversationEngine, mock_provider: MockProvider) -> None:
        """Context with turn_buffer is passed through to ContextBuilder."""
        context = {
            "turn_buffer": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ],
            "session_summary": "User greeted the assistant.",
        }
        response = engine.reply_with_provider(
            "Tell me about the weather",
            provider=mock_provider,
            context=context,
        )
        assert response == "Mock response"
        assert mock_provider.last_messages is not None
        contents = [m["content"] for m in mock_provider.last_messages]
        assert "Hi" in contents
        assert "Hello!" in contents
        assert "Tell me about the weather" in contents

    def test_falls_back_to_last_context(self, engine: ConversationEngine, mock_provider: MockProvider) -> None:
        """When no explicit context, falls back to engine.last_context."""
        engine.last_context = {
            "turn_buffer": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Response"},
            ],
        }
        response = engine.reply_with_provider(
            "Continue",
            provider=mock_provider,
            context=None,
        )
        assert response == "Mock response"
        assert mock_provider.last_messages is not None
        assert "Earlier" in str(mock_provider.last_messages)

    def test_messages_include_system_prompt(self, engine: ConversationEngine, mock_provider: MockProvider) -> None:
        """The message list must contain a system prompt carrying THIS tree's persona.

        The persona is deployment configuration (``.antigona/SOUL.md`` / ``.antigona/AGENTS.md``),
        which a sanitized public tree deliberately does not ship.  The contract asserted here is
        therefore the product contract, not a literal copied from one deployment:

        * every tree: the prompt is a ``system`` message that identifies the agent;
        * a tree that configures a persona: the prompt must carry that persona's **first and last**
          non-empty lines verbatim, so dropping the persona, truncating it to its first line, or
          replacing it fails the test.

        With no persona configured there is nothing to compare against, so only that last part is
        skipped — the test never invents a pass.
        """
        response = engine.reply_with_provider("Any question", provider=mock_provider)
        assert response == "Mock response"
        assert mock_provider.last_messages is not None
        system_msg = mock_provider.last_messages[0]
        assert system_msg["role"] == "system"
        assert "Antigona" in system_msg["content"]

        from antigona.core import paths

        # Read the persona the product itself would inject, through the same resolver the
        # ContextBuilder/PersonalityManager uses.  Both files are checked when both exist, so
        # dropping either one from the prompt is detected.  Reading the files directly (instead
        # of constructing a PersonalityManager) keeps the test free of side effects: the manager
        # creates .antigona/personalities/ inside the checkout on construction.
        local_dir = paths.project_local_dir()
        sources = [
            (name, (local_dir / name).read_text(encoding="utf-8"))
            for name in ("SOUL.md", "AGENTS.md")
            if (local_dir / name).is_file()
        ]
        configured = [
            (name, [line.strip() for line in text.splitlines() if line.strip()])
            for name, text in sources
        ]
        configured = [(name, lines) for name, lines in configured if lines]
        if not configured:
            pytest.skip(
                "no persona source in this tree (.antigona/SOUL.md|AGENTS.md are deployment-local "
                "and are not published); the persona-marker contract cannot be asserted here"
            )

        for name, lines in configured:
            for edge, marker in (("first", lines[0]), ("last", lines[-1])):
                assert marker in system_msg["content"], (
                    f"the system prompt must carry this tree's persona verbatim: the {edge} line "
                    f"{marker[:60]!r} of the configured {name} is missing from it"
                )


# --- Tests: Pure LLM conversation (no rule-based) ------------------------------


class TestPureLLMMode:
    """Every input goes to the LLM provider - no rule-based shortcuts."""

    def test_all_inputs_use_provider(self, engine_with_mock: ConversationEngine, mock_provider: MockProvider) -> None:
        """Every input, including greetings and thanks, goes to the provider."""
        for text in ["Hi", "Who are you?", "Thanks", "Bye"]:
            mock_provider.last_messages = None
            response = engine_with_mock.reply(text)
            assert response == "Mock response", (
                f"Expected provider response for '{text}', got: {response}"
            )
            assert mock_provider.last_messages is not None, (
                f"Provider should have been called for '{text}'"
            )

    def test_without_provider_raises(self, engine: ConversationEngine) -> None:
        """Without a provider, reply() raises ValueError."""
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider", return_value=None
        ):
            with pytest.raises(ValueError, match="No active LLM provider"):
                engine.reply("Hi")

    def test_without_provider_free_text_raises(self, engine: ConversationEngine) -> None:
        """Without a provider, any text raises ValueError."""
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider", return_value=None
        ):
            with pytest.raises(ValueError, match="No active LLM provider"):
                engine.reply("Tell me about Python")


# --- Tests: _generate_with_provider -------------------------------------------


class TestGenerateWithProvider:
    """Tests for the internal _generate_with_provider method."""

    def test_delegates_to_reply_with_provider(self) -> None:
        """_generate_with_provider should delegate to reply_with_provider."""
        provider = MockProvider()
        engine = ConversationEngine(provider=provider)
        called = False

        original = engine.reply_with_provider

        def patched(text: str, provider=None, context=None) -> str:  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            assert provider is not None
            return original(text, provider=provider, context=context)

        engine.reply_with_provider = patched  # type: ignore[assignment]
        engine._generate_with_provider("test")
        assert called, "reply_with_provider was not called"

    def test_with_configured_provider(self, mock_provider: MockProvider) -> None:
        """_generate_with_provider should use the engine's configured provider."""
        engine = ConversationEngine(provider=mock_provider)
        response = engine._generate_with_provider("How are you?")
        assert response == "Mock response"
        assert mock_provider.last_messages is not None

    def test_provider_failure_propagates(self) -> None:
        """When provider.generate() fails, the exception propagates."""
        failing = MockProvider(response="err", fail=True)
        engine = ConversationEngine(provider=failing)
        with pytest.raises(RuntimeError, match="err"):
            engine._generate_with_provider("Something complex")


# --- Tests: chitchat_reply -----------------------------------------------------


class TestChitchatReply:
    """Tests for the standalone chitchat_reply() function."""

    def test_with_provider_returns_llm_response(self) -> None:
        """chitchat_reply with a provider should return LLM response."""
        mock = MockProvider(response="This is an LLM response!")
        reply = chitchat_reply("Tell me about quantum physics", provider=mock)
        assert reply == "This is an LLM response!"
        assert mock.last_messages is not None
        assert mock.last_messages[-1]["content"] == "Tell me about quantum physics"

    def test_without_provider_uses_default(self) -> None:
        """chitchat_reply without provider should use ProviderResolver.get_provider."""
        mock = MockProvider(response="Response from default")
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider", return_value=mock
        ):
            reply = chitchat_reply("Hi")
        assert reply == "Response from default"
        assert mock.last_messages is not None

    def test_without_provider_no_key_raises(self) -> None:
        """chitchat_reply without provider and no default raises ValueError."""
        with patch(
            "antigona.providers.resolver.ProviderResolver.get_provider", return_value=None
        ):
            with pytest.raises(ValueError, match="No active LLM provider"):
                chitchat_reply("Hi")


# --- Tests: is_chitchat_or_noise -----------------------------------------------


class TestIsChitchatOrNoiseUnchanged:
    """is_chitchat_or_noise remains unchanged (still rule-based)."""

    def test_greeting(self) -> None:
        from antigona.conversation.engine import is_chitchat_or_noise
        assert is_chitchat_or_noise("Hi") is True

    def test_empty(self) -> None:
        from antigona.conversation.engine import is_chitchat_or_noise
        assert is_chitchat_or_noise("") is True

    def test_task_shell(self) -> None:
        from antigona.conversation.engine import is_chitchat_or_noise
        assert is_chitchat_or_noise("shell: ls -la") is False
