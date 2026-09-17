"""DAY 1 regression tests — non-task messages must NOT create a flow.

These tests were originally a red-spec (TDD against a then-missing intent
router).  The routing layer has since landed: the bot's ``text_handler`` routes
every message through ``process_user_input`` (InputPipeline), and the
``IntentRouter`` classifies chat/ambiguous input as *conversation* / *clarify*
— never a task flow.  These tests now lock in that current, verified behavior:

  - ``is_chitchat_or_noise`` is a pure length heuristic (empty or <=3 chars).
  - ``chitchat_reply`` is LLM-based — no canned fallbacks.
  - Routing a greeting / identity question / noise / bare verb through the real
    ``text_handler`` produces no flow and never calls the legacy ``post_flow``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from antigona.channels.telegram.bot import (
    TelegramBot,
    chitchat_reply,
    is_chitchat_or_noise,
)
from antigona.input_pipeline.models import ProcessingOutcome, ProcessingResult
from antigona.security.owner_identity import OwnerIdentity

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def bot_instance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TelegramBot]:
    """Return a TelegramBot with a temp SQLite DB and offline reply plumbing."""
    monkeypatch.setenv("ANTIGONA_ENABLE_STARTUP_RECOVERY", "0")
    # These probes exercise routing, not the owner gate — accept every user.
    monkeypatch.setattr(OwnerIdentity, "is_owner", lambda self, user_id: True)
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
    )
    # Keep the presenter/reply machinery offline and deterministic.
    bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    bot.bot.edit_message_text = AsyncMock()
    bot.bot.delete_message = AsyncMock()
    bot.operation_presenter._debounce = 0
    bot.event_bus.publish = AsyncMock()
    bot._store_session_message = AsyncMock()
    bot._update_last_response_time = MagicMock()
    bot._handle_plan_or_actions = AsyncMock(return_value=None)
    bot._operation_final_receipt = AsyncMock(return_value=None)
    bot._publish_operation_stage = AsyncMock()
    bot._publish_operation_final = AsyncMock(return_value=False)
    try:
        yield bot
    finally:
        await bot.close()
        await bot.gateway_client.close()


def _make_message(text: str) -> MagicMock:
    """Build a minimal Message mock with the given text."""
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = "private"
    msg.message_id = 1
    msg.from_user = MagicMock()
    msg.from_user.id = 99999
    # Production contract: the addressing gate ignores messages *from bots*
    # (should_process_message returns False for from_user.is_bot). A plain user
    # message must carry is_bot=False or the mock's truthy MagicMock would be
    # misread as a bot and the message dropped before reaching the turn API.
    msg.from_user.is_bot = False
    msg.reply_to_message = None
    msg.bot = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.answer = AsyncMock()
    msg.answer.__name__ = "answer"
    msg.html_text = text
    return msg


def _text_handler(bot: TelegramBot):
    """Locate the real text handler by name (see operation_lifecycle_hardening)."""
    return next(
        handler.callback
        for handler in bot.router.message.handlers
        if handler.callback.__name__ == "text_handler"
    )


def _conv_result(response_text: str) -> ProcessingResult:
    """A conversation-final pipeline result — chat input, never a task flow."""
    return ProcessingResult(
        success=True,
        task_id="conv",
        session_id="conv",
        response_text=response_text,
        error=None,
        correlation_id="corr",
        duration_ms=1.0,
        outcome=ProcessingOutcome.CONVERSATION_FINAL,
        terminal=False,
    )


def _mock_conversation_pipeline(
    monkeypatch: pytest.MonkeyPatch, response_text: str
) -> AsyncMock:
    """Route the text_handler through a fake conversation pipeline.

    Uses ``monkeypatch`` (auto-restored after each test) instead of
    ``unittest.mock.patch(...).start()``, which leaked the module-global
    ``process_user_input`` replacement into every later test in the suite.
    """
    pipeline = AsyncMock(return_value=_conv_result(response_text))
    monkeypatch.setattr(
        "antigona.channels.telegram.bot.process_user_input", pipeline
    )
    return pipeline


# ─── Test 1: Greeting should NOT create a flow ──────────────────────────────
# Step 4 (манифест): бот не фильтрует болтовню локально — весь текст уходит
# в единый Gateway Turn API, а ядро (AntigonaBrain) решает: разговор или
# задача. «Не создаёт флоу» — обязанность ядра, проверяем, что бот не
# вызывает никаких локальных post_flow/submit и просто рендерит ответ.


@pytest.mark.asyncio
async def test_greeting_привет_does_not_create_flow(
    bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Привет' уходит в ядро как разговор — бот не создаёт флоу локально."""
    message = _make_message("Привет")
    turn_mock = AsyncMock(
        return_value={
            "reply": "Привет! 👋 Чем могу помочь?",
            "session_id": "telegram:12345",
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }
    )
    monkeypatch.setattr(
        bot_instance.gateway_client, "send_dialogue_turn", turn_mock
    )

    with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
        await _text_handler(bot_instance)(message)

    mock_post.assert_not_awaited()
    turn_mock.assert_awaited_once()
    # Ответ рендерится через presenter (а не сырым send_message от бота).
    bot_instance.bot.send_message.assert_not_awaited()


# ─── Test 2: Identity question should NOT create a flow ──────────────────────


@pytest.mark.asyncio
async def test_question_кто_ты_does_not_create_flow(
    bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Кто ты?' — разговор; ядро отвечает, бот не создаёт флоу."""
    message = _make_message("Кто ты?")
    turn_mock = AsyncMock(
        return_value={
            "reply": "Я Antigona — твой AI-агент.",
            "session_id": "telegram:12345",
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }
    )
    monkeypatch.setattr(
        bot_instance.gateway_client, "send_dialogue_turn", turn_mock
    )

    with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
        await _text_handler(bot_instance)(message)

    mock_post.assert_not_awaited()
    turn_mock.assert_awaited_once()
    bot_instance.bot.send_message.assert_not_awaited()


# ─── Test 3: Single-character noise should NOT create a flow ────────────────


@pytest.mark.asyncio
async def test_noise_ы_does_not_create_flow(
    bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Ы' — разговор; ядро классифицирует как noise, бот не создаёт флоу."""
    message = _make_message("Ы")
    turn_mock = AsyncMock(
        return_value={
            "reply": "Ты написал просто «Ы» 😄",
            "session_id": "telegram:12345",
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }
    )
    monkeypatch.setattr(
        bot_instance.gateway_client, "send_dialogue_turn", turn_mock
    )

    with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
        await _text_handler(bot_instance)(message)

    mock_post.assert_not_awaited()
    turn_mock.assert_awaited_once()
    bot_instance.bot.send_message.assert_not_awaited()


# ─── Test 4: Known slash command should NOT create a flow ───────────────────


@pytest.mark.asyncio
async def test_slash_setllm_does_not_create_flow(
    bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Обычный текст со слешем' уходит в ядро; флоу создаёт только ядро."""
    message = _make_message("/setllm")
    turn_mock = AsyncMock(
        return_value={
            "reply": "Команда обработана ядром.",
            "session_id": "telegram:12345",
            "response_type": "control",
            "flow_id": None,
            "requires_approval": False,
            "verified": None,
        }
    )
    monkeypatch.setattr(
        bot_instance.gateway_client, "send_dialogue_turn", turn_mock
    )

    with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
        await _text_handler(bot_instance)(message)

    mock_post.assert_not_awaited()
    turn_mock.assert_awaited_once()
    bot_instance.bot.send_message.assert_not_awaited()


# ─── Test 5: Short action verb without context should NOT create a flow ─────


@pytest.mark.asyncio
async def test_verb_проверь_does_not_create_flow(
    bot_instance: TelegramBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Проверь' без контекста — ядро попросит уточнить, флоу не создаётся."""
    message = _make_message("Проверь")
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
    monkeypatch.setattr(
        bot_instance.gateway_client, "send_dialogue_turn", turn_mock
    )

    with patch.object(bot_instance, "post_flow", new=AsyncMock()) as mock_post:
        await _text_handler(bot_instance)(message)

    mock_post.assert_not_awaited()
    turn_mock.assert_awaited_once()
    bot_instance.bot.send_message.assert_not_awaited()


# ─── Direct unit tests for is_chitchat_or_noise (current semantics) ─────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", True),          # empty → noise
        ("   ", True),       # whitespace → noise
        ("Ы", True),         # single char → noise
        ("?", True),         # punctuation → noise
        ("S", True),         # single char → noise
        ("Привет", False),   # >3 chars → real message
        ("Кто ты?", False),  # >3 chars → real message
        ("/setllm", False),  # command text → real message
        ("Проверь", False),  # action verb → real message
        ("создай файл hello.txt", False),  # task → real message
    ],
    ids=["empty", "whitespace", "ы", "question_mark", "s", "privet",
         "kto_ty", "setllm", "prover", "task"],
)
def test_is_chitchat_or_noise_length_heuristic(text: str, expected: bool) -> None:
    """is_chitchat_or_noise is a pure length heuristic: empty or <=3 chars."""
    assert is_chitchat_or_noise(text) is expected


# ─── Edge-case test: chitchat_reply is LLM-based, not canned ─────────────────


def test_chitchat_reply_routes_to_provider() -> None:
    """chitchat_reply forwards input to the LLM provider — no canned fallback."""
    from antigona.providers.mock import MockProvider

    mock = MockProvider(responses=["Ответ от LLM провайдера"])
    reply = chitchat_reply("Ы", provider=mock)
    assert reply == "Ответ от LLM провайдера"
    assert "Не понял" not in reply


@pytest.mark.parametrize("text", ["Ы", "Кто ты?", "Привет"])
def test_chitchat_reply_returns_nonempty_string(text: str) -> None:
    """Every chat input produces a (mock) LLM reply — never empty."""
    from antigona.providers.mock import MockProvider

    reply = chitchat_reply(text, provider=MockProvider(responses=["ответ"]))
    assert isinstance(reply, str)
    assert len(reply) > 0
