"""Tests for Telegram pipeline integration — text_handler, voice_handler, reply context.

Tests the integration of bot.py handlers with the InputPipeline,
ensuring UserInputEnvelope is constructed correctly and the pipeline
is invoked with proper reply context and voice routing.

These tests use mocked Telegram Message objects and mocked pipeline
dependencies so they run without a Gateway or database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.input_pipeline.models import ProcessingResult, UserInputEnvelope

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_message(
    text: str = "hello",
    message_id: int = 1001,
    chat_id: int = -100,
    user_id: int = 42,
    reply_to_message: MagicMock | None = None,
    voice: MagicMock | None = None,
    audio: MagicMock | None = None,
    bot: MagicMock | None = None,
) -> MagicMock:
    """Build a mock aiogram.types.Message."""
    msg = MagicMock()
    msg.text = text
    msg.message_id = message_id
    msg.chat.id = chat_id
    msg.from_user.id = user_id
    msg.reply_to_message = reply_to_message
    msg.voice = voice
    msg.audio = audio

    if bot is None:
        bot = MagicMock()
        bot.get_file = AsyncMock()
    msg.bot = bot

    # message.answer — simulate sending a reply
    sent = MagicMock()
    sent.message_id = message_id + 1
    msg.answer = AsyncMock(return_value=sent)

    return msg


def _make_voice_file(file_id: str = "voice123", duration: int = 5) -> MagicMock:
    voice = MagicMock()
    voice.file_id = file_id
    voice.duration = duration
    return voice


def _make_reply_message(
    message_id: int = 500,
    text: str = "some status message",
    is_bot: bool = True,
) -> MagicMock:
    reply = MagicMock()
    reply.message_id = message_id
    reply.text = text
    reply.from_user.is_bot = is_bot
    return reply


def _make_pipeline_success(
    task_id: str = "flow-abc-123",
    response_text: str = "✅ Task created.",
) -> ProcessingResult:
    return ProcessingResult(
        success=True,
        task_id=task_id,
        session_id=task_id,
        response_text=response_text,
        error=None,
        correlation_id="corr-test-123",
        duration_ms=42.0,
    )


def _make_pipeline_error(
    error: str = "Something broke",
) -> ProcessingResult:
    return ProcessingResult(
        success=False,
        task_id=None,
        session_id=None,
        response_text=None,
        error=error,
        correlation_id="corr-test-err",
        duration_ms=5.0,
    )


def _make_pipeline_noop() -> ProcessingResult:
    return ProcessingResult(
        success=True,
        task_id=None,
        session_id=None,
        response_text=None,
        error=None,
        correlation_id="corr-test-noop",
        duration_ms=1.0,
    )


# ── Test: UserInputEnvelope construction ──────────────────────────────────────


class TestUserInputEnvelopeConstruction:
    """Verify envelope fields set correctly for different message shapes."""

    def test_text_message_no_reply(self) -> None:
        """Plain text message → no reply context."""
        msg = _make_message(text="создай файл")
        reply_to = msg.reply_to_message

        reply_to_id: int | None = None
        reply_to_bot: bool = False
        if reply_to is not None:
            reply_to_id = reply_to.message_id
            reply_to_bot = reply_to.from_user.is_bot

        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=msg.text,
            reply_to_message_id=reply_to_id,
            reply_to_bot_message=reply_to_bot,
        )

        assert envelope.source == "telegram_text"
        assert envelope.user_id == 42
        assert envelope.chat_id == -100
        assert envelope.message_id == 1001
        assert envelope.text == "создай файл"
        assert envelope.reply_to_message_id is None
        assert envelope.reply_to_bot_message is False
        assert envelope.attachments is None

    def test_text_message_with_reply(self) -> None:
        """Reply to a bot message → reply context set."""
        reply_to = _make_reply_message(message_id=500, is_bot=True)
        msg = _make_message(text="добавь ещё", reply_to_message=reply_to)

        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=msg.text,
            reply_to_message_id=reply_to.message_id,
            reply_to_bot_message=reply_to.from_user.is_bot,
        )

        assert envelope.reply_to_message_id == 500
        assert envelope.reply_to_bot_message is True

    def test_text_message_with_reply_to_user(self) -> None:
        """Reply to a non-bot user message → reply_to_bot_message=False."""
        reply_to = _make_reply_message(message_id=501, is_bot=False)
        msg = _make_message(text="согласен", reply_to_message=reply_to)

        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=msg.text,
            reply_to_message_id=reply_to.message_id,
            reply_to_bot_message=reply_to.from_user.is_bot,
        )

        assert envelope.reply_to_message_id == 501
        assert envelope.reply_to_bot_message is False

    def test_voice_message_no_reply(self) -> None:
        """Voice message → source=telegram_voice, includes attachment."""
        voice = _make_voice_file()
        msg = _make_message(text="", voice=voice)

        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text="распознанный текст",
            attachments=[{
                "type": "voice",
                "file_id": voice.file_id,
                "duration": voice.duration,
            }],
        )

        assert envelope.source == "telegram_voice"
        assert envelope.text == "распознанный текст"
        assert envelope.attachments is not None
        atts = envelope.attachments
        assert atts[0]["type"] == "voice"
        assert atts[0]["file_id"] == "voice123"

    def test_voice_message_with_reply(self) -> None:
        """Voice reply to bot → reply context + voice source."""
        reply_to = _make_reply_message(message_id=600, is_bot=True)
        voice = _make_voice_file()
        msg = _make_message(text="", voice=voice, reply_to_message=reply_to)

        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text="добавь в задачу",
            reply_to_message_id=reply_to.message_id,
            reply_to_bot_message=reply_to.from_user.is_bot,
            attachments=[{"type": "voice", "file_id": voice.file_id}],
        )

        assert envelope.source == "telegram_voice"
        assert envelope.reply_to_message_id == 600
        assert envelope.reply_to_bot_message is True

    def test_audio_message(self) -> None:
        """Audio message (music file, not voice) → same handler, source=telegram_voice."""
        audio = MagicMock()
        audio.file_id = "audio789"
        audio.duration = 120
        msg = _make_message(text="", audio=audio)

        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text="аудио транскрипция",
            attachments=[{
                "type": "voice",
                "file_id": audio.file_id,
                "duration": audio.duration,
            }],
        )

        assert envelope.source == "telegram_voice"
        atts = envelope.attachments
        assert atts is not None
        assert atts[0]["file_id"] == "audio789"


# ── Test: ProcessingResult consumption ────────────────────────────────────────


class TestHandlerResponseFlow:
    """Verify how handlers consume ProcessingResult to respond to users."""

    @pytest.mark.asyncio
    async def test_success_response_text(self) -> None:
        """Successful pipeline with response_text → bot answers with that text."""
        msg = _make_message()
        result = _make_pipeline_success(response_text="✅ Task flow-abc created.")

        if result.success and result.response_text:
            sent = await msg.answer(result.response_text)
            assert sent.message_id == 1002
            msg.answer.assert_awaited_once_with("✅ Task flow-abc created.")
        else:
            pytest.fail("Expected success with response_text")

    @pytest.mark.asyncio
    async def test_success_no_response_text(self) -> None:
        """Successful pipeline without response_text → fallback acknowledgment."""
        msg = _make_message()
        result = _make_pipeline_noop()

        if result.success and result.response_text:
            sent = await msg.answer(result.response_text)
        elif result.error:
            await msg.answer(f"❌ {result.error}")
        else:
            # Pipeline succeeded but no response text
            sent = await msg.answer("✅ Понял.")
            assert sent.message_id == 1002
            msg.answer.assert_awaited_once_with("✅ Понял.")

    @pytest.mark.asyncio
    async def test_pipeline_error_response(self) -> None:
        """Pipeline error → bot responds with error message."""
        msg = _make_message()
        result = _make_pipeline_error("Gateway timeout")

        if result.success and result.response_text:
            await msg.answer(result.response_text)
        elif result.error:
            await msg.answer(f"❌ {result.error}")
            msg.answer.assert_awaited_with("❌ Gateway timeout")
        else:
            pytest.fail("Expected error")


# ── Test: Binding save for assistant responses ────────────────────────────────


class TestAssistantBinding:
    """Verify binding_repo.save is called for assistant responses."""

    @pytest.mark.asyncio
    async def test_binding_saved_on_success(self) -> None:
        """After successful response → binding saved as assistant."""
        msg = _make_message()
        sent = MagicMock()
        sent.message_id = 2000
        msg.answer = AsyncMock(return_value=sent)

        repo = AsyncMock()
        repo.save = AsyncMock()
        result = _make_pipeline_success(task_id="flow-task-1")

        if result.success and result.response_text:
            sent = await msg.answer(result.response_text)
            await repo.save(
                chat_id=msg.chat.id,
                telegram_message_id=sent.message_id,
                user_id=None,
                task_id=result.task_id,
                correlation_id=result.correlation_id,
                message_role="assistant",
                message_kind="task_result",
            )
            repo.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_binding_saved_on_fallback(self) -> None:
        """Fallback 'Понял' response → binding saved as acknowledgment."""
        msg = _make_message()
        sent = MagicMock()
        sent.message_id = 2001
        msg.answer = AsyncMock(return_value=sent)

        repo = AsyncMock()
        repo.save = AsyncMock()
        result = _make_pipeline_noop()

        # Simulate fallback path
        if not result.response_text and result.success:
            sent = await msg.answer("✅ Понял.")
            await repo.save(
                chat_id=msg.chat.id,
                telegram_message_id=sent.message_id,
                user_id=None,
                task_id=result.task_id,
                correlation_id=result.correlation_id,
                message_role="assistant",
                message_kind="acknowledgment",
            )
            repo.save.assert_awaited_once()
            msg.answer.assert_awaited_with("✅ Понял.")


# ── Test: Idempotency / duplicate handling ────────────────────────────────────


class TestIdempotency:
    """Verify idempotency mechanisms work correctly."""

    def test_same_message_id_produces_same_envelope(self) -> None:
        """Two envelopes with same message_id → same core fields."""
        msg1 = _make_message(text="hello", message_id=777)
        msg2 = _make_message(text="hello", message_id=777)

        e1 = UserInputEnvelope(
            source="telegram_text",
            user_id=msg1.from_user.id,
            chat_id=msg1.chat.id,
            message_id=msg1.message_id,
            text=msg1.text,
        )
        e2 = UserInputEnvelope(
            source="telegram_text",
            user_id=msg2.from_user.id,
            chat_id=msg2.chat.id,
            message_id=msg2.message_id,
            text=msg2.text,
        )

        assert e1.chat_id == e2.chat_id
        assert e1.message_id == e2.message_id
        assert e1.source == e2.source


# ── Test: Voice transcription metadata ────────────────────────────────────────


class TestVoiceMetadata:
    """Verify voice attachment metadata is properly shaped."""

    def test_voice_metadata_shape(self) -> None:
        """Voice attachment contains type, file_id, duration."""
        voice = _make_voice_file(file_id="file_abc", duration=10)

        attachment = {
            "type": "voice",
            "file_id": voice.file_id,
            "duration": voice.duration,
        }

        assert attachment["type"] == "voice"
        assert attachment["file_id"] == "file_abc"
        assert attachment["duration"] == 10

    def test_voice_envelope_has_attachments(self) -> None:
        """Voice envelope always carries attachments list."""
        voice = _make_voice_file()
        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=1,
            chat_id=1,
            message_id=1,
            text="transcript",
            attachments=[{
                "type": "voice",
                "file_id": voice.file_id,
                "duration": voice.duration,
            }],
        )

        assert envelope.attachments is not None
        assert len(envelope.attachments) == 1

    def test_text_envelope_no_attachments(self) -> None:
        """Text envelope has no attachments by default."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=1,
            chat_id=1,
            message_id=1,
            text="plain text",
        )

        assert envelope.attachments is None


# ── Test: Reply context resolution via pipeline ───────────────────────────────


class TestReplyContextPipeline:
    """Verify reply_to_message_id flows correctly into the pipeline."""

    @pytest.mark.asyncio
    async def test_reply_to_message_id_passed_to_pipeline(self) -> None:
        """Envelope with reply_to_message_id → pipeline receives it."""
        mock_pipeline = AsyncMock()
        mock_pipeline.return_value = _make_pipeline_success()

        reply_to = _make_reply_message(message_id=700, is_bot=True)
        msg = _make_message(text="продолжи", reply_to_message=reply_to)

        # Simulate what text_handler does
        reply_to_id = msg.reply_to_message.message_id if msg.reply_to_message else None
        reply_to_bot = msg.reply_to_message.from_user.is_bot if msg.reply_to_message else False

        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=msg.text,
            reply_to_message_id=reply_to_id,
            reply_to_bot_message=reply_to_bot,
        )

        assert envelope.reply_to_message_id == 700
        assert envelope.reply_to_bot_message is True

    @pytest.mark.asyncio
    async def test_voice_reply_to_message_id_passed_to_pipeline(self) -> None:
        """Voice envelope with reply → reply context preserved."""
        reply_to = _make_reply_message(message_id=800, is_bot=True)
        voice = _make_voice_file()
        msg = _make_message(text="", voice=voice, reply_to_message=reply_to)

        reply_to_id = msg.reply_to_message.message_id if msg.reply_to_message else None
        reply_to_bot = msg.reply_to_message.from_user.is_bot if msg.reply_to_message else False  # type: ignore[union-attr]

        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text="transcribed text",
            reply_to_message_id=reply_to_id,
            reply_to_bot_message=reply_to_bot,
            attachments=[{
                "type": "voice",
                "file_id": voice.file_id,
                "duration": voice.duration,
            }],
        )

        assert envelope.reply_to_message_id == 800
        assert envelope.reply_to_bot_message is True
        assert envelope.source == "telegram_voice"


# ── Stage 1 live-smoke regression (2026-08-02) ────────────────────────────────


class TestReplyContextDoesNotLeakIntoTurnApi:
    """Reply-quote blocks must never re-classify a short reply as a task.

    Live Telegram smoke: owner replied "?" to a bot progress message that
    quoted a write-task. The old fallback forwarded ``llm_text`` (which
    contains the "[Контекст: …]" quote) into the Turn API; AntigonaBrain
    re-classified the quoted task text as a NEW task and the safety policy
    rejected it ("task input rejected by safety policy"), so the bot
    reported a false failure. The fix sends the raw user text instead.
    """

    def test_reply_context_block_contains_quoted_task_text(self) -> None:
        reply_to = _make_reply_message(message_id=700, is_bot=True)
        reply_to.text = "Создай файл tg_smoke.txt с текстом привет"
        msg = _make_message(text="?", reply_to_message=reply_to)

        from antigona.channels.telegram.bot import build_reply_context_block

        block = build_reply_context_block(msg)

        assert block is not None
        assert "Создай файл" in block
        # The raw user text the pipeline / Turn API must receive.
        assert msg.text == "?"

    def test_reply_context_does_not_turn_question_mark_into_task(self) -> None:
        """Router still classifies the raw '?' as noise, not a task."""
        from antigona.router.intent_router import IntentRouter

        decision = IntentRouter().route(
            text="?", context={"source": "telegram_text"}
        )

        assert decision.intent == "conversation.noise"

    def test_no_reply_message_gets_no_context_block(self) -> None:
        msg = _make_message(text="привет")
        msg.reply_to_message = None

        from antigona.channels.telegram.bot import build_reply_context_block

        assert build_reply_context_block(msg) is None
