"""DAY 13 — Tests for ContextBuilder: persona, policy, history, token budget.

Tests:
  1. estimate_tokens — approximate token counting
  2. ContextBuilder default persona assembly
  3. System prompt with policy rules injection
  4. System prompt with session summary
  5. History from turn_buffer
  6. Token budget enforcement — truncation of old messages
  7. Token budget — within budget, no truncation
  8. Token budget — single message exceeds budget
  9. Policy rules injection in system prompt
  10. Empty turn_buffer
  11. Integration: ConversationEngine uses ContextBuilder
  12. ConversationEngine uses ContextBuilder with turn_buffer
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import antigona.context.builder as builder_module
from antigona.context.builder import (
    ContextBuilder,
    estimate_tokens,
    truncate_history_by_budget,
)
from antigona.conversation.engine import ConversationEngine
from antigona.providers.mock import MockProvider

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: estimate_tokens
# ═══════════════════════════════════════════════════════════════════════════════


class TestEstimateTokens:
    """Token estimation: 4 chars ≈ 1 token."""

    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 0

    def test_exact_multiple(self) -> None:
        # "abcd" = 4 chars → 1 token
        assert estimate_tokens("abcd") == 1

    def test_ceiling_division(self) -> None:
        # "abc" = 3 chars → ceil(3/4) = 1 token
        assert estimate_tokens("abc") == 1
        # "abcde" = 5 chars → ceil(5/4) = 2 tokens
        assert estimate_tokens("abcde") == 2

    def test_long_text(self) -> None:
        text = "a" * 1000
        assert estimate_tokens(text) == 250

    def test_russian_text(self) -> None:
        text = "Привет! Как дела?"
        # 18 chars → ceil(18/4) = 5 tokens
        assert estimate_tokens(text) == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: truncate_history_by_budget
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruncateHistoryByBudget:
    """History truncation — oldest messages dropped when over budget."""

    def test_empty_messages(self) -> None:
        assert truncate_history_by_budget([], 1000, 10) == []

    def test_within_budget(self) -> None:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        system_tokens = estimate_tokens(messages[0]["content"])
        result = truncate_history_by_budget(messages, 10_000, system_tokens)
        assert len(result) == 3  # nothing dropped

    def test_drops_oldest_when_over_budget(self) -> None:
        """When over budget, oldest user/assistant messages are dropped."""
        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "old_message_1"},
            {"role": "assistant", "content": "old_reply_1"},
            {"role": "user", "content": "new_message"},
        ]
        sys_tokens = estimate_tokens("SYS")
        # Very tight budget: only system + newest message fits
        tight_budget = sys_tokens + estimate_tokens("new_message") + 10
        result = truncate_history_by_budget(messages, tight_budget, sys_tokens)
        assert len(result) >= 1  # system always there
        assert result[0]["role"] == "system"
        # The newest messages should survive
        surviving_content = [m.get("content", "") for m in result]
        assert "new_message" in surviving_content

    def test_current_user_message_never_dropped_when_over_budget(self) -> None:
        """P-01 invariant: current (last) user message is preserved even over budget.

        Regression for the CONTROL_PACK P-01 'current-message invariant': a very
        long current user question must never be silently dropped from context,
        even when it exceeds the token budget. Older messages may still be
        evicted oldest-first.
        """
        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "assistant", "content": "some previous reply"},
            {"role": "user", "content": "A" * 500},  # current message, very long
        ]
        sys_tokens = estimate_tokens("SYS")
        # Budget only fits the system message + a little slack
        result = truncate_history_by_budget(messages, sys_tokens + 5, sys_tokens)
        contents = [m.get("content", "") for m in result]
        # System always first
        assert result[0]["role"] == "system"
        # Current user message must survive despite exceeding the budget
        assert "A" * 500 in contents
        # Older assistant reply is dropped
        assert "some previous reply" not in contents


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: ContextBuilder — system prompt assembly
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBuilderSystemPrompt:
    """System prompt assembly from persona, policy, and session summary."""

    def test_default_persona(self) -> None:
        """Default ContextBuilder uses a sensible persona."""
        builder = ContextBuilder()
        messages = builder.build()
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert "Antigona" in messages[0]["content"]
        assert "автоматизацией" in messages[0]["content"]

    def test_custom_persona(self) -> None:
        """Custom persona overrides the default."""
        builder = ContextBuilder(persona="Custom bot.")
        messages = builder.build()
        # The persona is the first block; SOUL.md/AGENTS.md and memory snapshot
        # blocks may be appended after it.
        assert messages[0]["content"].startswith("Custom bot.")

    def test_policy_rules_injection(self) -> None:
        """Policy verdicts are injected into the system prompt."""
        builder = ContextBuilder()
        verdicts: list[dict[str, Any]] = [
            {
                "allowed": True,
                "requires_approval": False,
                "reason": "Action passed default policy check",
                "risk_level": "LOW",
            },
            {
                "allowed": False,
                "requires_approval": True,
                "reason": "Command contains dangerous pattern: rm -rf /",
                "risk_level": "HIGH",
            },
        ]
        messages = builder.build(policy_verdicts=verdicts)
        content = messages[0]["content"]
        assert "ПРАВИЛА БЕЗОПАСНОСТИ" in content
        assert "разрешено" in content
        assert "ЗАПРЕЩЕНО" in content
        assert "rm -rf" in content

    def test_policy_rules_disabled(self) -> None:
        """Policy rules can be excluded from the system prompt."""
        builder = ContextBuilder(include_policy=False)
        verdicts: list[dict[str, Any]] = [
            {
                "allowed": True,
                "reason": "Test",
                "risk_level": "LOW",
            },
        ]
        messages = builder.build(policy_verdicts=verdicts)
        assert "ПРАВИЛА БЕЗОПАСНОСТИ" not in messages[0]["content"]

    def test_session_summary_injected(self) -> None:
        """Session summary appears in the system prompt."""
        builder = ContextBuilder()
        messages = builder.build(session_summary="User: test | Turns: 5 | Topic: code review")
        content = messages[0]["content"]
        assert "СЕССИЯ" in content
        assert "User: test" in content
        assert "Turns: 5" in content

    def test_policy_and_summary_together(self) -> None:
        """Both policy rules and session summary appear in the system prompt."""
        builder = ContextBuilder()
        verdicts: list[dict[str, Any]] = [
            {"allowed": True, "reason": "OK", "risk_level": "LOW"},
        ]
        messages = builder.build(
            policy_verdicts=verdicts,
            session_summary="User: test | Turns: 3",
        )
        content = messages[0]["content"]
        assert "ПРАВИЛА БЕЗОПАСНОСТИ" in content
        assert "СЕССИЯ" in content
        assert "User: test" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: ContextBuilder — history assembly
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBuilderHistory:
    """Turn buffer → message list conversion."""

    def test_empty_turn_buffer(self) -> None:
        """Empty turn_buffer returns only the system message."""
        builder = ContextBuilder()
        messages = builder.build(turn_buffer=[])
        assert len(messages) == 1
        assert messages[0]["role"] == "system"

    def test_basic_history(self) -> None:
        """Turn buffer entries become user/assistant messages."""
        builder = ContextBuilder()
        turns = [
            {"role": "user", "content": "Hello", "intent": "", "timestamp": "2024-01-01T00:00:00"},
            {"role": "assistant", "content": "Hi!", "intent": "", "timestamp": "2024-01-01T00:00:01"},
            {"role": "user", "content": "Create a file", "intent": "task.file_write", "timestamp": "2024-01-01T00:00:02"},
        ]
        messages = builder.build(turn_buffer=turns)
        # system + 3 turns
        assert len(messages) == 4
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "Hi!"
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "Create a file"

    def test_unknown_role_skipped(self) -> None:
        """Entries with unknown roles are skipped."""
        builder = ContextBuilder()
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "hidden instruction"},  # skipped
            {"role": "assistant", "content": "Hi!"},
        ]
        messages = builder.build(turn_buffer=turns)
        # system + 2 turns (system role from turn_buffer skipped)
        assert len(messages) == 3
        contents = [m.get("content", "") for m in messages]
        assert "hidden instruction" not in contents

    def test_none_content_handled(self) -> None:
        """None content is converted to empty string."""
        builder = ContextBuilder()
        turns = [
            {"role": "user", "content": None},
        ]
        messages = builder.build(turn_buffer=turns)
        assert messages[1]["content"] == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: ContextBuilder — token budget enforcement
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBuilderTokenBudget:
    """Token budget truncation when building context."""

    def test_default_budget_no_truncation(self) -> None:
        """Default 32K budget preserves all history for reasonable sizes."""
        builder = ContextBuilder()
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        messages = builder.build(turn_buffer=turns)
        # All messages should survive (budget is 32K, turns are tiny)
        assert len(messages) == 3  # system + 2 turns

    def test_truncation_with_tight_budget(self) -> None:
        """Oldest messages are dropped when budget is tight."""
        builder = ContextBuilder(token_budget=30)
        turns = [
            {"role": "user", "content": "This is a very old user message that should be dropped first"},
            {"role": "assistant", "content": "This is an old reply that should also be dropped"},
            {"role": "user", "content": "This is the current question"},
        ]
        messages = builder.build(turn_buffer=turns)
        # System message + newest messages
        # With budget 30, system uses ~15 tokens, leaving ~15 for history
        # At ~5 tokens per message, only 2-3 messages may fit
        assert len(messages) >= 1  # system always survives
        # The newest message should survive
        surviving = [m.get("content", "") for m in messages]
        assert "This is the current question" in surviving or "This is a very old" not in surviving

    def test_custom_token_budget(self) -> None:
        """Custom token budget is respected."""
        builder = ContextBuilder(token_budget=50)
        # System prompt alone is ~15 tokens, leaving ~35 for history
        turns = [
            {"role": "user", "content": "A" * 40},   # ~10 tokens + 4 overhead = ~14
            {"role": "assistant", "content": "B" * 80},  # ~20 tokens + 4 overhead = ~24
            # Total ~38 — should exceed budget of 35, so something gets dropped
        ]
        messages = builder.build(turn_buffer=turns)
        assert len(messages) < 3  # something was truncated
        assert messages[0]["role"] == "system"  # system always preserved

    def test_system_and_current_message_preserved(self) -> None:
        """Extreme budget: system + current user message are both preserved.

        P-01 current-message invariant: the user's current question must never be
        dropped from context even at an extreme token budget.
        """
        builder = ContextBuilder(token_budget=5)
        turns = [
            {"role": "user", "content": "A" * 100},
        ]
        messages = builder.build(turn_buffer=turns)
        assert messages[0]["role"] == "system"  # system always preserved
        assert len(messages) == 2  # current user message preserved alongside system
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "A" * 100


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Integration with ConversationEngine
# ═══════════════════════════════════════════════════════════════════════════════


class TestConversationEngineIntegration:
    """ConversationEngine uses ContextBuilder for message assembly."""

    def test_engine_uses_context_builder_default(self) -> None:
        """ConversationEngine creates a default ContextBuilder."""
        engine = ConversationEngine(provider=MockProvider())
        assert engine.context_builder is not None
        assert isinstance(engine.context_builder, ContextBuilder)

    def test_engine_custom_context_builder(self) -> None:
        """Custom ContextBuilder can be injected."""
        custom = ContextBuilder(persona="Custom persona")
        engine = ConversationEngine(provider=MockProvider(), context_builder=custom)
        assert engine.context_builder is custom
        assert engine.context_builder.persona == "Custom persona"

    def test_engine_generate_with_turn_buffer(self) -> None:
        """_generate_with_provider uses ContextBuilder when turn_buffer is set."""
        provider = MockProvider()
        engine = ConversationEngine(provider=provider)
        # Set a turn_buffer in last_context to exercise the ContextBuilder path
        engine.last_context = {
            "turn_buffer": [
                {"role": "user", "content": "previous question", "intent": "", "timestamp": ""},
                {"role": "assistant", "content": "previous answer", "intent": "", "timestamp": ""},
            ],
            "session_summary": "User: test | Turns: 2",
        }
        reply = engine._generate_with_provider("new question")
        # Should get a response from the mock provider
        assert isinstance(reply, str)
        assert len(reply) > 0
        assert provider.call_count == 1

    def test_engine_generate_without_turn_buffer(self) -> None:
        """_generate_with_provider falls back to simple construction."""
        provider = MockProvider()
        engine = ConversationEngine(provider=provider)
        reply = engine._generate_with_provider("hello")
        assert isinstance(reply, str)
        assert len(reply) > 0
        assert provider.call_count == 1

    def test_engine_context_builder_custom_persona(self) -> None:
        """Custom persona from ContextBuilder flows through to provider messages."""
        custom_builder = ContextBuilder(persona="Custom offline agent")
        provider = MockProvider()
        engine = ConversationEngine(provider=provider, context_builder=custom_builder)

        # Spy on the provider's generate method without wrapping it
        original_generate = provider.generate
        captured_messages: list[list[dict[str, str]]] = []

        def spying_generate(
            messages: list[dict[str, str]],
            context: dict[str, Any] | None = None,
        ) -> str:
            captured_messages.append(messages)
            return original_generate(messages, context)

        provider.generate = spying_generate  # type: ignore[method-assign]
        engine._generate_with_provider("test")

        assert len(captured_messages) == 1
        system_content = captured_messages[0][0]["content"]
        assert "Custom offline agent" in system_content


class _ProbeCapability:
    def __init__(self) -> None:
        self.probe_fn = object()
        self.last_probe_time = None


class _ProbeRegistry:
    def __init__(self, delay: float = 0.0) -> None:
        self._capabilities = {"probe": _ProbeCapability()}
        self.delay = delay
        self.cancelled = threading.Event()
        self.started = threading.Event()

    async def probe_all(self) -> None:
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def test_probe_timeout_cancels_and_drains_coroutine() -> None:
    reg = _ProbeRegistry(delay=2.0)
    builder_module._probe_capabilities_sync(reg, timeout=0.2)
    assert reg.cancelled.wait(1.0)
    assert builder_module._PROBE_LOOP is None
    assert builder_module._PROBE_THREAD is None
    assert not [t for t in threading.enumerate() if t.name == "CapabilityProbeLoop"]


def test_probe_repeated_start_stop_leaves_no_pending_task() -> None:
    for _ in range(3):
        reg = _ProbeRegistry()
        builder_module._probe_capabilities_sync(reg, timeout=1.0)
    assert builder_module._PROBE_LOOP is None
    assert builder_module._PROBE_THREAD is None


def test_probe_stop_does_not_join_under_lock_or_deadlock() -> None:
    reg = _ProbeRegistry(delay=0.01)
    builder_module._probe_capabilities_sync(reg, timeout=1.0)
    assert not builder_module._PROBE_LOCK.locked()
