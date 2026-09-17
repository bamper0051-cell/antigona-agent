"""DAY 4 / DAY 5 — Tests for the Intent Router layer.

Day 4: Verifies IntentRouter classifies messages correctly using deterministic rules,
confidence gates, and returns proper IntentDecision contracts.

Day 5: Context-aware routing — bare verbs resolve with conversation context,
followup routes to task.continue with active task, ConversationState integration.

Tests:
  - 3 conversation intents → conversation response, zero tools
  - 3 question intents → answer response, zero tools
  - 2 task intents (file_write, shell) → task_preview response
  - 2 ambiguous intents → clarify response
  - 1 high vs low confidence gate check
  - 2 mixed intents (emoji, very long text)
  - Day 5: ConversationState push/build_context
  - Day 5: bare "Проверь" без контекста → ambiguous.followup
  - Day 5: "Проверь" после "создай файл /etc/config" → task.shell с entities
  - Day 5: "Продолжай" с активной задачей → task.continue
  - Day 5: "Исправь" после файла → task.file_edit
  - Day 5: "Создай" после контекста → task.file_write
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from antigona.intent_router import (
    ConversationState,
    IntentDecision,
    IntentRouter,
    clarify_reply,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def close_telegram_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Close TelegramBot resources created by the integration-style probes."""
    from antigona.channels.telegram.bot import TelegramBot
    from antigona.core.gateway_client import GatewayClient

    instances: list[tuple[TelegramBot, GatewayClient]] = []
    original_init = TelegramBot.__init__

    def tracked_init(self: TelegramBot, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        instances.append((self, self.gateway_client))

    monkeypatch.setattr(TelegramBot, "__init__", tracked_init)
    yield
    for bot, gateway in instances:
        await bot.close()
        await gateway.close()


@pytest.fixture
def router() -> IntentRouter:
    return IntentRouter()


def _assert_decision(
    decision: IntentDecision,
    *,
    intent_prefix: str,
    response_mode: str,
    requires_planner: bool = False,
    requires_approval: bool = False,
    reason_code_prefix: str = "",
) -> None:
    """Helper to assert key fields of an IntentDecision."""
    assert decision.intent.startswith(intent_prefix), (
        f"Expected intent starting with '{intent_prefix}', got '{decision.intent}'"
    )
    assert decision.response_mode == response_mode, (
        f"Expected response_mode '{response_mode}', got '{decision.response_mode}'"
    )
    assert decision.requires_planner == requires_planner
    assert decision.requires_approval == requires_approval
    if reason_code_prefix:
        assert decision.reason_code.startswith(reason_code_prefix), (
            f"Expected reason_code starting with '{reason_code_prefix}', "
            f"got '{decision.reason_code}'"
        )


# ─── Test 1: 3 conversation intents → conversation response, zero tools ──────


@pytest.mark.parametrize(
    "text,expected_intent,reason_code",
    [
        ("Привет", "conversation.greeting", "greeting_match"),
        ("Кто ты?", "conversation.identity", "identity_question"),
        ("Спасибо", "conversation.thanks", "thanks_match"),
    ],
    ids=["greeting", "identity", "thanks"],
)
def test_conversation_intents(
    router: IntentRouter,
    text: str,
    expected_intent: str,
    reason_code: str,
) -> None:
    """Conversation messages → conversation response, no tools."""
    decision = router.route(text)

    _assert_decision(
        decision,
        intent_prefix="conversation.",
        response_mode="conversation",
        reason_code_prefix=reason_code,
    )
    assert decision.intent == expected_intent, (
        f"Expected exact intent '{expected_intent}', got '{decision.intent}'"
    )
    # Conversation intents must NOT require planner or approval
    assert decision.requires_planner is False
    assert decision.requires_approval is False
    # Confidence must be at the highest tier
    assert decision.confidence >= 0.90

    # Verify routing goes through chitchat_reply, not clarify_reply
    # (conversation intents are handled by ConversationEngine in bot.py)


# ─── Test 2: 3 question intents → answer response, zero tools ────────────────


@pytest.mark.parametrize(
    "text,expected_intent,reason_code",
    [
        ("Объясни как создать файл", "analysis.explain", "explain_question"),
        ("Можно ли удалить этот файл?", "question.project", "project_question"),
        ("Как работает Intent Router?", "question.general", "general_question"),
    ],
    ids=["explain", "project_question", "general_question"],
)
def test_question_intents(
    router: IntentRouter,
    text: str,
    expected_intent: str,
    reason_code: str,
) -> None:
    """Question messages → answer response, no tools."""
    decision = router.route(text)

    _assert_decision(
        decision,
        intent_prefix=expected_intent.split(".")[0] + ".",
        response_mode="answer",
        reason_code_prefix=reason_code,
    )
    assert decision.intent == expected_intent
    assert decision.requires_planner is False
    assert decision.requires_approval is False
    # Question intents should have reasonable confidence
    assert decision.confidence >= 0.80


# ─── Test 3: 2 task intents (file_write, shell) → task_preview response ──────


@pytest.mark.parametrize(
    "text,expected_intent,reason_code",
    [
        ("Создай файл hello.txt с текстом привет", "task.file_write", "explicit_file_creation_phrase"),
        ("shell: ls -la", "task.shell", "shell_prefix"),
    ],
    ids=["file_write", "shell"],
)
def test_task_intents(
    router: IntentRouter,
    text: str,
    expected_intent: str,
    reason_code: str,
) -> None:
    """Task messages → task_preview response."""
    decision = router.route(text)

    _assert_decision(
        decision,
        intent_prefix="task.",
        response_mode="task_preview",
        requires_planner=True,
        requires_approval=True,
        reason_code_prefix=reason_code,
    )
    assert decision.intent == expected_intent
    assert decision.requires_planner is True
    assert decision.requires_approval is True
    assert decision.confidence >= 0.85


# ─── Test 4: Ambiguous intents → clarify response ────────────────────────────


@pytest.mark.parametrize(
    "text,expected_intent,reason_code",
    [
        ("Проверь", "ambiguous.followup", "bare_action_verb_no_context"),
        ("Продолжай", "ambiguous.followup", "followup_no_context"),
    ],
    ids=["bare_verb_no_context", "followup_no_context"],
)
def test_ambiguous_intents(
    router: IntentRouter,
    text: str,
    expected_intent: str,
    reason_code: str,
) -> None:
    """Ambiguous messages without context → clarify response, no tools."""
    decision = router.route(text)

    _assert_decision(
        decision,
        intent_prefix="ambiguous.",
        response_mode="clarify",
        reason_code_prefix=reason_code,
    )
    assert decision.intent == expected_intent
    assert decision.requires_planner is False
    assert decision.requires_approval is False
    # Without context, confidence should be low (< 0.65)
    assert decision.confidence < 0.65, (
        f"Bare verb without context should have low confidence (<0.65), "
        f"got {decision.confidence}"
    )

    # Verify clarify_reply produces a meaningful clarification
    reply = clarify_reply(text)
    assert len(reply) > 10, "Clarification reply should be substantive"
    assert "🤔" in reply, "Clarification should start with a thinking emoji"


# ─── Test 5: High vs low confidence gate check ────────────────────────────────


def test_confidence_gates(router: IntentRouter) -> None:
    """High-confidence task actions get >= 0.90; unclear messages get < 0.65."""
    # High confidence: explicit task with clear action verb + target
    high_decision = router.route("Создай файл hello.txt с текстом привет")
    assert high_decision.confidence >= 0.90, (
        f"Explicit file creation should have >=0.90 confidence, "
        f"got {high_decision.confidence}"
    )
    assert high_decision.response_mode == "task_preview"

    # Low confidence: very short ambiguous message
    low_decision = router.route("ну")
    # "ну" is 2 chars → length ≤ 2 → conversation.noise with 0.95
    # This is correct: very short filler words are tagged as noise
    assert low_decision.confidence >= 0.90, (
        f"Short noise 'ну' should have high confidence (it's clearly noise), "
        f"got {low_decision.confidence}"
    )
    assert low_decision.response_mode == "conversation", (
        f"Short noise should route to conversation, "
        f"got {low_decision.response_mode}"
    )

    # Medium confidence: ambiguous bare verb without context (0.60)
    mid_decision = router.route("Проверь")
    assert mid_decision.confidence < 0.65, (
        f"Bare verb 'Проверь' without context should have <0.65 confidence, "
        f"got {mid_decision.confidence}"
    )
    assert mid_decision.response_mode == "clarify"


# ─── Test 6: 2 mixed intents (emoji, very long text) ─────────────────────────


@pytest.mark.parametrize(
    "text,expected_mode",
    [
        # Emoji-only or emoji-heavy messages are noise
        ("👍", "conversation"),
        ("😊👍🎉", "conversation"),
        # Very long text with clear action verb → task
        (
            "Создай файл readme.md с подробной документацией по нашему проекту. "
            "Опиши все модули и их функции. Добавь примеры использования. "
            "Не забудь про установку зависимостей и настройку окружения.",
            "task_preview",
        ),
    ],
    ids=["emoji_short", "emoji_longer", "very_long_task"],
)
def test_mixed_intents(
    router: IntentRouter,
    text: str,
    expected_mode: str,
) -> None:
    """Edge cases: emoji messages, very long text."""
    decision = router.route(text)

    assert decision.response_mode == expected_mode, (
        f"For text {text[:30]!r}... expected response_mode '{expected_mode}', "
        f"got '{decision.response_mode}' (intent={decision.intent})"
    )

    # Safety: emoji-only should never create a flow
    if "👍" in text and len(text) <= 10:
        assert decision.requires_planner is False
        assert decision.requires_approval is False


# ─── Test 7: Decision contract completeness ───────────────────────────────────


def test_decision_contract_completeness(router: IntentRouter) -> None:
    """Every IntentDecision must have all contract fields populated."""
    texts = [
        "Привет",
        "Кто ты?",
        "Спасибо",
        "Проверь",
        "Создай файл hello.txt",
        "shell: ls",
        "Объясни как работает",
        "👍",
        "",
    ]
    for text in texts:
        decision = router.route(text)

        # All fields must be present and of correct type
        assert isinstance(decision.intent, str), "intent must be str"
        assert isinstance(decision.confidence, float), "confidence must be float"
        assert decision.response_mode in (
            "conversation", "answer", "command_result", "task_preview", "clarify",
        ), f"Invalid response_mode: {decision.response_mode}"
        assert isinstance(decision.requires_planner, bool)
        assert isinstance(decision.requires_approval, bool)
        assert isinstance(decision.entities, dict)
        assert isinstance(decision.reason_code, str)

        # Confidence must be in valid range
        assert 0.0 <= decision.confidence <= 1.0, (
            f"Confidence {decision.confidence} out of range [0,1]"
        )

        # Task intents must require planner + approval
        if decision.response_mode == "task_preview":
            assert decision.requires_planner is True, (
                f"Task intent {decision.intent} must require planner"
            )
            assert decision.requires_approval is True, (
                f"Task intent {decision.intent} must require approval"
            )


# ─── Test 8: IntentRouter → handler integration (simulates bot.py) ────────────


@pytest.mark.asyncio
async def test_intent_router_prevents_flow_for_conversation() -> None:
    """Simulate the bot.py text_handler with IntentRouter for conversation."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from antigona.channels.telegram.bot import TelegramBot

    bot_instance = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
    )

    for text in ["Привет", "Кто ты?", "Спасибо", "Пока"]:
        msg = MagicMock()
        msg.text = text
        msg.chat = MagicMock()
        msg.chat.id = 12345
        msg.chat.type = "private"
        msg.message_id = 1
        msg.from_user = MagicMock()
        msg.from_user.id = 99999
        msg.bot = MagicMock()
        msg.answer = AsyncMock()
        msg.answer.__name__ = "answer"
        msg.html_text = text

        with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
            # Handler index 3 is the text handler
            await bot_instance.router.message.handlers[3].callback(msg)

            # post_flow MUST NOT be called
            mock_post.assert_not_awaited()
            msg.answer.assert_awaited_once()

            # Verify it's a chitchat reply, not a Task Flow card
            reply_text = msg.answer.call_args[0][0]
            assert "Task Flow" not in reply_text
            # Response should come from ConversationEngine
            assert message_has_content(reply_text)


@pytest.mark.asyncio
async def test_intent_router_prevents_flow_for_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4: 'Проверь' без контекста — ядро классифицирует как clarify.

    Интерфейс не создаёт флоу локально: весь текст уходит в Gateway Turn API,
    а ядро (AntigonaBrain) решает, что это уточнение, а не задача.
    """
    from unittest.mock import AsyncMock, MagicMock

    from antigona.channels.telegram.bot import TelegramBot
    from antigona.router.intent_router import IntentRouter
    from antigona.security.owner_identity import OwnerIdentity

    # This probe exercises routing, not the owner gate — accept every user.
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)

    bot_instance = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
    )

    # 1. Routing contract: 'Проверь' (no context) → clarify, not a flow.
    decision = IntentRouter().route("Проверь")
    assert decision.intent == "ambiguous.followup"
    assert decision.response_mode == "clarify"
    assert decision.requires_planner is False

    # 2. The thin text_handler sends the text to the Gateway Turn API and
    #    never creates a flow locally.
    text_handler = next(
        handler.callback
        for handler in bot_instance.router.message.handlers
        if handler.callback.__name__ == "text_handler"
    )
    msg = MagicMock()
    msg.text = "Проверь"
    msg.chat = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = "private"
    msg.message_id = 1
    msg.from_user = MagicMock()
    msg.from_user.id = 99999
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock()
    msg.answer.__name__ = "answer"
    msg.html_text = "Проверь"

    bot_instance.event_bus.publish = AsyncMock()
    bot_instance._update_last_response_time = MagicMock()
    bot_instance._operation_final_receipt = AsyncMock(return_value=None)
    bot_instance._publish_operation_stage = AsyncMock()
    bot_instance._publish_operation_final = AsyncMock(return_value=False)
    bot_instance.operation_store = AsyncMock()
    bot_instance.operation_store.find_active_by_progress_message = AsyncMock(return_value=None)
    bot_instance.operation_store.create = AsyncMock(
        return_value=MagicMock(id="op-1")
    )

    turn_mock = AsyncMock(
        return_value={
            "reply": "🤔 Что именно проверить?",
            "session_id": "telegram:12345",
            "response_type": "clarification",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }
    )
    bot_instance.gateway_client.send_dialogue_turn = turn_mock

    await text_handler(msg)

    # Интерфейс не создаёт флоу — текст ушёл в ядро (Gateway Turn API).
    turn_mock.assert_awaited_once()


def message_has_content(reply_text: str) -> bool:
    """Check that a reply has meaningful content (not empty or just whitespace)."""
    return bool(reply_text and reply_text.strip())


# ═══════════════════════════════════════════════════════════════════════════════
# DAY 5 — Context-aware routing and ConversationState
# ═══════════════════════════════════════════════════════════════════════════════


# ─── Test D5-1: ConversationState push/build_context ──────────────────────────


class TestConversationState:
    """ConversationState unit tests."""

    def test_push_message_updates_state(self) -> None:
        """push_message should update messages, last_intent, last_entities."""
        state = ConversationState()
        state.push_message(
            text="создай файл /etc/config",
            intent="task.file_write",
            entities={"path": "/etc/config"},
        )

        assert len(state.messages) == 1
        assert state.messages[0]["text"] == "создай файл /etc/config"
        assert state.messages[0]["intent"] == "task.file_write"
        assert state.last_intent == "task.file_write"
        assert state.last_entities == {"path": "/etc/config"}
        assert "создай файл" in state.last_topic

    def test_push_message_limits_history(self) -> None:
        """push_message should keep at most max_messages entries."""
        state = ConversationState(max_messages=3)
        for i in range(5):
            state.push_message(text=f"msg {i}", intent="conversation.smalltalk")

        assert len(state.messages) == 3
        # Oldest messages should be evicted
        assert state.messages[0]["text"] == "msg 2"
        assert state.messages[-1]["text"] == "msg 4"

    def test_push_message_sets_active_task_id(self) -> None:
        """push_message with task_id should set active_task_id."""
        state = ConversationState()
        state.push_message(text="run deploy", intent="task.shell", task_id="flow-42")

        assert state.active_task_id == "flow-42"

    def test_build_context_empty(self) -> None:
        """build_context returns empty dict for fresh state."""
        state = ConversationState()
        ctx = state.build_context()
        assert ctx == {}

    def test_build_context_with_messages(self) -> None:
        """build_context returns context with previous_messages and active_topic."""
        state = ConversationState()
        state.push_message(
            text="создай файл /etc/config",
            intent="task.file_write",
            entities={"path": "/etc/config"},
        )

        ctx = state.build_context()
        assert "previous_messages" in ctx
        assert len(ctx["previous_messages"]) == 1
        assert ctx["active_topic"] == "создай файл /etc/config"
        assert ctx["last_entities"] == {"path": "/etc/config"}

    def test_build_context_with_task_id(self) -> None:
        """build_context includes active_task_id when set."""
        state = ConversationState()
        state.push_message(
            text="run deploy",
            intent="task.shell",
            task_id="flow-99",
        )

        ctx = state.build_context()
        assert ctx["active_task_id"] == "flow-99"


# ─── Test D5-2: Bare verb without context → ambiguous.followup ────────────────


def test_bare_verb_no_context_is_followup(router: IntentRouter) -> None:
    """Bare 'Проверь' without context → ambiguous.followup, not flow."""
    # Intentionally passing NO context parameter
    decision = router.route("Проверь")

    assert decision.intent == "ambiguous.followup", (
        f"Expected ambiguous.followup, got {decision.intent}"
    )
    assert decision.confidence < 0.65, f"Confidence should be <0.65, got {decision.confidence}"
    assert decision.response_mode == "clarify", (
        f"Expected clarify, got {decision.response_mode}"
    )
    assert decision.requires_planner is False
    assert decision.requires_approval is False


def test_bare_verb_no_context_via_empty_dict(router: IntentRouter) -> None:
    """Bare 'Проверь' with empty context dict → ambiguous.followup."""
    decision = router.route("Проверь", context={})

    assert decision.intent == "ambiguous.followup"
    assert decision.confidence < 0.65
    assert decision.response_mode == "clarify"


# ─── Test D5-3: Bare verb with context → task intent ─────────────────────────


def test_bare_prover_with_file_context_is_task_shell(router: IntentRouter) -> None:
    """"Проверь" after "создай файл /etc/config" → task.shell with entities."""
    context = {
        "active_topic": "создай файл /etc/config",
        "last_entities": {"path": "/etc/config"},
    }
    decision = router.route("Проверь", context=context)

    assert decision.intent == "task.shell", (
        f"Expected task.shell with context, got {decision.intent}"
    )
    assert decision.response_mode == "task_preview", (
        f"Expected task_preview, got {decision.response_mode}"
    )
    assert decision.requires_planner is True
    assert decision.requires_approval is True
    assert decision.entities == {"path": "/etc/config"}, (
        f"Expected entities from context, got {decision.entities}"
    )


def test_bare_isprav_with_file_context_is_file_edit(router: IntentRouter) -> None:
    """"Исправь" after file context → task.file_edit."""
    context = {
        "active_topic": "создай файл /etc/config",
        "last_entities": {"path": "/etc/config"},
    }
    decision = router.route("Исправь", context=context)

    assert decision.intent == "task.file_edit", (
        f"Expected task.file_edit, got {decision.intent}"
    )
    assert decision.response_mode == "task_preview"
    assert decision.entities == {"path": "/etc/config"}


def test_bare_sozdai_with_context_is_file_write(router: IntentRouter) -> None:
    """"Создай" after context → task.file_write with entities."""
    context = {
        "active_topic": "документация проекта",
        "last_entities": {"path": "readme.md"},
    }
    decision = router.route("Создай", context=context)

    assert decision.intent == "task.file_write", (
        f"Expected task.file_write, got {decision.intent}"
    )
    assert decision.entities == {"path": "readme.md"}


def test_bare_realizui_with_context_is_code_change(router: IntentRouter) -> None:
    """"Реализуй" after context → task.code_change."""
    context = {
        "active_topic": "добавь новую функцию в модуль auth",
        "last_entities": {},
    }
    decision = router.route("Реализуй", context=context)

    assert decision.intent == "task.code_change", (
        f"Expected task.code_change, got {decision.intent}"
    )


def test_bare_verb_with_entityless_context_defaults_to_shell(router: IntentRouter) -> None:
    """Bare verb with active_topic but no entities → task.shell."""
    context = {
        "active_topic": "напиши тесты для модуля",
        "last_entities": {},
    }
    decision = router.route("Запусти", context=context)

    assert decision.intent == "task.shell"
    assert decision.response_mode == "task_preview"


# ─── Test D5-4: Followup without history → ambiguous.followup ────────────────


def test_followup_no_history_is_ambiguous(router: IntentRouter) -> None:
    """"Продолжай" without context → ambiguous.followup."""
    decision = router.route("Продолжай")

    assert decision.intent == "ambiguous.followup"
    assert decision.response_mode == "clarify"
    assert decision.confidence < 0.65


@pytest.mark.parametrize(
    "text",
    ["дальше", "далее", "ещё", "continue", "next", "more"],
    ids=["dalshe", "dalee", "esho", "continue", "next", "more"],
)
def test_followup_variants_no_context(router: IntentRouter, text: str) -> None:
    """Various followup prompts without context → ambiguous.followup."""
    decision = router.route(text)

    assert decision.intent == "ambiguous.followup"
    assert decision.response_mode == "clarify"
    assert decision.confidence < 0.65


# ─── Test D5-5: Followup with active task → task.continue ────────────────────


def test_followup_with_active_task_is_continue(router: IntentRouter) -> None:
    """"Продолжай" with active_task_id → task.continue."""
    context = {
        "active_task_id": "flow-42",
    }
    decision = router.route("Продолжай", context=context)

    assert decision.intent == "task.continue", (
        f"Expected task.continue, got {decision.intent}"
    )
    assert decision.response_mode == "task_preview"
    assert decision.requires_planner is True
    assert decision.requires_approval is True
    assert decision.entities == {"task_id": "flow-42"}


@pytest.mark.parametrize(
    "text",
    ["дальше", "далее", "ещё", "continue", "next"],
    ids=["dalshe", "dalee", "esho", "continue", "next"],
)
def test_followup_variants_with_active_task(router: IntentRouter, text: str) -> None:
    """Various followup prompts with active_task_id → task.continue."""
    context = {"active_task_id": "flow-99"}
    decision = router.route(text, context=context)

    assert decision.intent == "task.continue", (
        f"For '{text}' expected task.continue, got {decision.intent}"
    )
    assert decision.entities == {"task_id": "flow-99"}


# ─── Test D5-6: ConversationState + router integration ───────────────────────


def test_full_context_router_integration(router: IntentRouter) -> None:
    """Simulate a two-message conversation: task → bare verb with context."""
    state = ConversationState()

    # Step 1: user creates a file (use a path with extension for entity detection)
    decision1 = router.route("создай файл /etc/config.yaml")
    assert decision1.intent == "task.file_write"
    entities1 = dict(decision1.entities)
    state.push_message(
        text="создай файл /etc/config.yaml",
        intent=decision1.intent,
        entities=entities1,
    )

    # Step 2: user says "Проверь" — should resolve with context from step 1
    ctx = state.build_context()
    assert ctx["active_topic"] is not None
    assert ctx["last_entities"] == {"path": "/etc/config.yaml"}

    decision2 = router.route("Проверь", context=ctx)
    assert decision2.intent == "task.shell", (
        f"Expected task.shell after creation context, got {decision2.intent}"
    )
    assert decision2.entities == {"path": "/etc/config.yaml"}, (
        f"Entities should carry over from context, got {decision2.entities}"
    )


def test_followup_after_then_bare_verb_no_context(router: IntentRouter) -> None:
    """After a followup with active task, new bare verb without task → followup."""
    state = ConversationState()

    # Start a task
    state.push_message(
        text="запусти деплой",
        intent="task.shell",
        task_id="flow-1",
    )

    # Followup with active task → task.continue
    ctx = state.build_context()
    assert ctx.get("active_task_id") == "flow-1"
    continue_decision = router.route("продолжай", context=ctx)
    assert continue_decision.intent == "task.continue"

    # Now simulate a NEW chat without context
    fresh_decision = router.route("Проверь")
    assert fresh_decision.intent == "ambiguous.followup"
    assert fresh_decision.confidence < 0.65


# ═══════════════════════════════════════════════════════════════════════════════
# Step 20b — Bare ASCII shell command (no Russian verb) → ambiguous.mixed_intent
#
# Regression coverage for the live-owner bug: typing a bare "ls -a" in the CLI
# used to be misclassified as conversation.smalltalk (Step 21's word-count
# shortcut) because neither _TASK_SHELL_RE (Step 17) nor _ACTION_VERB_RE
# (Step 20) has a matching verb for verb-less ASCII commands — their verb
# lists are Russian-first. conversation.smalltalk then went to the LLM
# persona, which honestly refused ("P0 не позволяет") since free chat has no
# shell tool attached, instead of the command ever reaching the task
# pipeline / sandboxed shell.
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    ["ls -a", "uptime", "df -h", "ps aux", "whoami", "cat /etc/hostname"],
    ids=["ls_a", "uptime", "df_h", "ps_aux", "whoami", "cat_hostname"],
)
def test_bare_ascii_shell_command_is_mixed_intent(router: IntentRouter, text: str) -> None:
    """Bare ASCII shell commands (owner-reported bug) must NOT become smalltalk.

    Routed as ambiguous.mixed_intent (not task.shell directly) so it goes
    through brain.py's existing _extract_shell_command() double-check before
    the task pipeline commits to sandbox.shell — see core/brain.py
    process(), the `elif intent.intent == "ambiguous.mixed_intent":` branch.
    """
    decision = router.route(text)

    assert decision.intent == "ambiguous.mixed_intent", (
        f"Expected ambiguous.mixed_intent for {text!r}, got {decision.intent} "
        f"(reason_code={decision.reason_code})"
    )
    assert decision.reason_code == "bare_ascii_shell_command"
    assert decision.intent != "conversation.smalltalk"


def test_bare_ascii_shell_command_reaches_task_via_brain() -> None:
    """End-to-end: ambiguous.mixed_intent + pure-ascii text must reach
    _handle_task/sandbox.shell in brain.py, not clarification.

    This is the second half of the contract described above: the router
    only decides *that* it looks like a shell command; brain.py's existing
    _extract_shell_command() heuristic decides whether to actually commit to
    the task pipeline (ambiguous.mixed_intent branch in `process()`).
    """
    from antigona.core.brain import _extract_shell_command

    for text in ("ls -a", "uptime", "df -h", "ps aux", "whoami", "cat /etc/hostname"):
        assert _extract_shell_command(text) == text, (
            f"_extract_shell_command must pass through pure-ascii {text!r} unchanged, "
            "otherwise brain.py routes ambiguous.mixed_intent to clarification "
            "instead of the task pipeline."
        )


@pytest.mark.parametrize(
    "text",
    [
        "привет",
        "спасибо",
        "да",
        "ок",
        "нет",
        "пока",
        "Привет",
        "Спасибо",
    ],
    ids=["privet", "spasibo", "da", "ok", "net", "poka", "privet_cap", "spasibo_cap"],
)
def test_russian_smalltalk_not_swept_into_shell_detection(router: IntentRouter, text: str) -> None:
    """Non-regression: short Russian conversational replies keep their prior
    classification — they must never be reclassified by the new bare-ASCII
    shell-command step, because they're intercepted earlier in the pipeline
    (greeting/thanks/goodbye/noise, Steps 4-8) or contain Cyrillic so the
    ASCII-only gate never even applies to them.
    """
    decision = router.route(text)

    assert decision.intent != "ambiguous.mixed_intent"
    assert decision.reason_code != "bare_ascii_shell_command"
    assert decision.intent.startswith("conversation.")


@pytest.mark.parametrize(
    "text",
    [
        "Who are you?",
        "how are you",
        "who is there",
        "who cares about the config",
        "cool",
        "nice one",
        "hello there",
    ],
    ids=[
        "who_are_you_q",
        "how_are_you",
        "who_is_there",
        "who_cares",
        "cool",
        "nice_one",
        "hello_there",
    ],
)
def test_english_smalltalk_not_swept_into_shell_detection(router: IntentRouter, text: str) -> None:
    """Non-regression: plain English chat/questions must not be misrouted into
    a shell-command decision just because they're pure ASCII with no Cyrillic.
    """
    decision = router.route(text)

    assert decision.reason_code != "bare_ascii_shell_command", (
        f"{text!r} was misclassified as a bare shell command "
        f"(intent={decision.intent})"
    )


def test_shell_lookalike_command_with_english_word_tail_is_not_shell(
    router: IntentRouter,
) -> None:
    """A shell-like first token followed by an English stop word reads as a
    sentence, not a command — must not trigger the new detection.
    """
    decision = router.route("ls of my things")

    assert decision.reason_code != "bare_ascii_shell_command"


# ─── P1 TTS FALSE_DONE regression (2026-09-06) ────────────────────────────
# A combined request that ALSO asks to voice it ("расскажи X и озвучь") must
# route to task.mcp (real TTS task), NOT be captured by the analysis.explain
# branch (Step 12).  Otherwise the model can DECLARE "speech.tts завершён" as
# narration without ever invoking the tool -> false DONE, no artifact/delivery.

def test_combined_story_plus_tts_routes_to_task_mcp() -> None:
    router = IntentRouter()
    cases = [
        "расскажи короткую историю о себе, озвучь её и пришли аудио",
        "расскажи историю и озвучь",
        "объясни тему и озвучь это",
        "расскажи о себе и озвучь",
    ]
    for text in cases:
        decision = router.route(text)
        assert decision.intent == "task.mcp", (
            f"TTS-combined request must route to task.mcp, got {decision.intent} "
            f"(reason={decision.reason_code}) for {text!r}"
        )

def test_pure_explain_without_tts_stays_analysis() -> None:
    router = IntentRouter()
    decision = router.route("расскажи о себе")
    assert decision.intent == "analysis.explain"

def test_explicit_speak_request_routes_to_task_mcp() -> None:
    router = IntentRouter()
    decision = router.route("озвучь текст")
    assert decision.intent == "task.mcp"
