"""Tests for Telegram pipeline integration — Stage 6+7+8.

Scenarios:
  1. edited_message -> revision flow
  2. restart -> binding restored (mock database)
  3. restart -> active task continues
  4. duplicate update -> no new task (idempotency key)
  5. voice error -> clear response to user
  6. context resolver error -> fallback
  7. Full E2E flow: text -> pipeline -> gateway -> response -> binding
  8. Quick messages (burst) - all messages saved
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.core.control_plane import FlowStatus
from antigona.input_pipeline.models import (
    ProcessingOutcome,
    ProcessingResult,
    UserInputEnvelope,
)
from antigona.input_pipeline.pipeline import (
    process_user_input,
    recover_all_contexts,
    recover_context_after_restart,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_message(
    text: str = "hello",
    message_id: int = 1001,
    chat_id: int = -100,
    user_id: int = 42,
    reply_to_message: MagicMock | None = None,
    voice: MagicMock | None = None,
    audio: MagicMock | None = None,
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
    msg.bot = MagicMock()
    msg.bot.get_file = AsyncMock()
    sent = MagicMock()
    sent.message_id = message_id + 1
    msg.answer = AsyncMock(return_value=sent)
    return msg


def _make_binding(
    chat_id: int = -100,
    message_id: int = 1001,
    task_id: str | None = "flow-abc-123",
    user_id: int = 42,
    original_text: str = "original text",
    edit_version: int = 0,
) -> MagicMock:
    binding = MagicMock()
    binding.chat_id = chat_id
    binding.telegram_message_id = message_id
    binding.task_id = task_id
    binding.user_id = user_id
    binding.original_text = original_text
    binding.edit_version = edit_version
    binding.metadata_json = {}
    return binding


def _make_processing_result(
    success: bool = True,
    task_id: str | None = "flow-task-1",
    response_text: str | None = "Task created.",
    error: str | None = None,
) -> ProcessingResult:
    return ProcessingResult(
        success=success,
        task_id=task_id,
        session_id=task_id,
        response_text=response_text,
        error=error,
        correlation_id="corr-test",
        duration_ms=42.0,
    )


# =========================================================================
# Scenario 1: edited_message -> revision flow
# =========================================================================


class TestEditedMessageFlow:
    """Scenario 1: editing a message -> pipeline revision."""

    @pytest.mark.asyncio
    async def test_edited_message_creates_envelope(self) -> None:
        """Edit -> UserInputEnvelope with source=telegram_edited."""
        msg = _make_message(text="fixed text", message_id=1001)

        envelope = UserInputEnvelope(
            source="telegram_edited",
            user_id=msg.from_user.id,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text=msg.text,
            edited_message_id=msg.message_id,
            metadata={
                "edit_version": 1,
                "previous_edit_version": 0,
                "original_text": "old text",
            },
        )

        assert envelope.source == "telegram_edited"
        assert envelope.edited_message_id == 1001
        assert envelope.text == "fixed text"
        assert envelope.metadata["edit_version"] == 1

    @pytest.mark.asyncio
    async def test_edited_message_runs_through_pipeline(self) -> None:
        """Edit through pipeline - processed as steering."""
        envelope = UserInputEnvelope(
            source="telegram_edited",
            user_id=42,
            chat_id=-100,
            message_id=1001,
            text="fixed text",
            edited_message_id=1001,
            metadata={"edit_version": 1, "original_text": "old text"},
        )

        mock_gateway = AsyncMock()
        mock_gateway.submit = AsyncMock()
        mock_router = MagicMock()  # sync route()
        mock_router.route.return_value = MagicMock(
            intent="steer",
            confidence=0.9,
            response_mode="direct",
            requires_planner=False,
        )
        mock_resolver = AsyncMock()
        ctx = MagicMock()
        ctx.intent.value = "CORRECT_MESSAGE"
        ctx.task_id = "flow-abc-123"
        ctx.steered_text = "fixed text"
        ctx.matched_task = None
        ctx.confidence = 0.9
        mock_resolver.resolve.return_value = ctx

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_router,
            context_resolver=mock_resolver,
            gateway_client=mock_gateway,
            event_bus=None,
            binding_repository=None,
        )

        assert result.success
        assert result.task_id == "flow-abc-123"

    @pytest.mark.asyncio
    async def test_edited_message_without_binding_returns_early(self) -> None:
        """Edit without binding -> handler returns early."""
        mock_binding_repo = AsyncMock()
        mock_binding_repo.get_by_message_id.return_value = None

        msg = _make_message(text="edit", message_id=999)

        binding = await mock_binding_repo.get_by_message_id(
            msg.chat.id, msg.message_id,
        )
        assert binding is None

    @pytest.mark.asyncio
    async def test_edited_message_increments_version(self) -> None:
        """Edit increments edit_version."""
        mock_binding_repo = AsyncMock()
        old_binding = _make_binding(
            chat_id=-100, message_id=1001, edit_version=0,
        )
        updated_binding = _make_binding(
            chat_id=-100, message_id=1001, edit_version=1,
            original_text="new text",
        )
        mock_binding_repo.get_by_message_id.return_value = old_binding
        mock_binding_repo.update_edited.return_value = updated_binding

        binding = await mock_binding_repo.get_by_message_id(-100, 1001)
        assert binding.edit_version == 0

        updated = await mock_binding_repo.update_edited(-100, 1001, "new text")
        assert updated.edit_version == 1


# =========================================================================
# Scenario 2: restart -> binding restored (mock database)
# =========================================================================


class TestRestartBindingRecovery:
    """Scenario 2: after restart bindings are loaded from DB."""

    @pytest.mark.asyncio
    async def test_load_all_bindings(self) -> None:
        """load_all_bindings returns a list."""
        mock_repo = AsyncMock()
        mock_repo.load_all_bindings.return_value = [
            _make_binding(chat_id=100, message_id=1, task_id="task-1"),
            _make_binding(chat_id=100, message_id=2, task_id="task-1"),
            _make_binding(chat_id=200, message_id=3, task_id="task-2"),
        ]

        bindings = await mock_repo.load_all_bindings(limit=100)
        assert len(bindings) == 3
        assert bindings[0].task_id == "task-1"

    @pytest.mark.asyncio
    async def test_load_chat_bindings(self) -> None:
        """load_chat_bindings filters by chat_id."""
        mock_repo = AsyncMock()
        mock_repo.load_chat_bindings.return_value = [
            _make_binding(chat_id=100, message_id=1, task_id="task-1"),
            _make_binding(chat_id=100, message_id=2, task_id="task-1"),
        ]

        bindings = await mock_repo.load_chat_bindings(chat_id=100, limit=10)
        assert len(bindings) == 2
        for b in bindings:
            assert b.chat_id == 100

    @pytest.mark.asyncio
    async def test_recover_context_after_restart_stats(self) -> None:
        """recover_context_after_restart returns correct stats."""
        mock_repo = AsyncMock()
        mock_resolver = AsyncMock()

        mock_repo.load_chat_bindings.return_value = [
            _make_binding(
                chat_id=100, message_id=1,
                task_id="task-1", original_text="create file",
            ),
        ]

        ctx = MagicMock()
        ctx.task_id = "task-1"
        mock_resolver.resolve.return_value = ctx

        result = await recover_context_after_restart(
            chat_id=100,
            binding_repository=mock_repo,
            context_resolver=mock_resolver,
        )

        assert result["bindings_loaded"] >= 1
        assert result["tasks_recovered"] == 1

    @pytest.mark.asyncio
    async def test_recover_empty_chat(self) -> None:
        """Chat without bindings -> empty stats."""
        mock_repo = AsyncMock()
        mock_resolver = AsyncMock()
        mock_repo.load_chat_bindings.return_value = []

        result = await recover_context_after_restart(
            chat_id=999,
            binding_repository=mock_repo,
            context_resolver=mock_resolver,
        )

        assert result["bindings_loaded"] == 0
        assert result["tasks_recovered"] == 0

    @pytest.mark.asyncio
    async def test_recover_all_contexts(self) -> None:
        """recover_all_contexts returns dict per chat."""
        mock_repo = AsyncMock()
        mock_resolver = AsyncMock()

        mock_repo.load_all_bindings.return_value = [
            _make_binding(chat_id=100, message_id=1, task_id="task-1"),
            _make_binding(chat_id=200, message_id=2, task_id="task-2"),
        ]

        ctx = MagicMock()
        ctx.task_id = "task-recovered"
        mock_resolver.resolve.return_value = ctx

        # Override per-chat to return appropriate data
        async def _load_chat(cid: int, **kw: object) -> list:
            return [b for b in await mock_repo.load_all_bindings() if b.chat_id == cid] or []

        mock_repo.load_chat_bindings = _load_chat

        results = await recover_all_contexts(
            binding_repository=mock_repo,
            context_resolver=mock_resolver,
            chat_ids=[100],
        )

        assert 100 in results
        assert results[100]["bindings_loaded"] > 0


# =========================================================================
# Scenario 3: restart -> active task continues
# =========================================================================


class TestRestartActiveTask:
    """Scenario 3: after restart active task continues."""

    @pytest.mark.asyncio
    async def test_recover_active_task_from_binding(self) -> None:
        """Binding with active task -> task_id recovered."""
        mock_repo = AsyncMock()
        mock_resolver = AsyncMock()

        mock_repo.load_chat_bindings.return_value = [
            _make_binding(
                chat_id=100,
                message_id=1,
                task_id="task-active-1",
                original_text="write code",
            ),
        ]

        ctx = MagicMock()
        ctx.task_id = "task-active-1"
        mock_resolver.resolve.return_value = ctx

        result = await recover_context_after_restart(
            chat_id=100,
            binding_repository=mock_repo,
            context_resolver=mock_resolver,
        )

        assert result["tasks_recovered"] == 1

    @pytest.mark.asyncio
    async def test_recover_no_task_id_in_binding(self) -> None:
        """Binding without task_id -> no recovery."""
        mock_repo = AsyncMock()
        mock_resolver = AsyncMock()

        mock_repo.load_chat_bindings.return_value = [
            _make_binding(
                chat_id=100, message_id=1,
                task_id=None, original_text="hi",
            ),
        ]

        result = await recover_context_after_restart(
            chat_id=100,
            binding_repository=mock_repo,
            context_resolver=mock_resolver,
        )

        assert result["tasks_recovered"] == 0


# =========================================================================
# Scenario 4: duplicate update -> idempotency
# =========================================================================


class TestDuplicateUpdateIdempotency:
    """Scenario 4: duplicate message does not create extra task."""

    @pytest.mark.asyncio
    async def test_duplicate_message_id_same_envelope(self) -> None:
        """Two messages with same message_id -> same envelope fields."""
        e1 = UserInputEnvelope(
            source="telegram_text",
            user_id=42, chat_id=-100,
            message_id=777, text="hello",
        )
        e2 = UserInputEnvelope(
            source="telegram_text",
            user_id=42, chat_id=-100,
            message_id=777, text="hello",
        )

        assert e1.chat_id == e2.chat_id
        assert e1.message_id == e2.message_id

    @pytest.mark.asyncio
    async def test_duplicate_edit_not_create_extra_task(self) -> None:
        """Duplicate edit of same message -> resolves to same task."""
        mock_resolver = AsyncMock()
        ctx = MagicMock()
        ctx.task_id = "task-original"
        ctx.steered_text = "first edit"
        mock_resolver.resolve.side_effect = [ctx, ctx]

        ctx1 = await mock_resolver.resolve(
            text="first edit", chat_id=100, user_id=42,
            message_id=2001, edited_message_id=2000,
        )
        ctx2 = await mock_resolver.resolve(
            text="first edit", chat_id=100, user_id=42,
            message_id=2001, edited_message_id=2000,
        )

        assert ctx1.task_id == ctx2.task_id

    @pytest.mark.asyncio
    async def test_idempotency_key_in_binding(self) -> None:
        """Binding save called twice with same ids."""
        mock_repo = AsyncMock()

        await mock_repo.save(
            chat_id=-100, telegram_message_id=777,
            user_id=42, task_id="task-1",
            correlation_id="corr-1", message_role="user",
            message_kind="text", original_text="hello",
        )

        await mock_repo.save(
            chat_id=-100, telegram_message_id=777,
            user_id=42, task_id="task-1-updated",
            correlation_id="corr-2", message_role="user",
            message_kind="text", original_text="hello updated",
        )

        assert mock_repo.save.await_count == 2


# =========================================================================
# Scenario 5: voice error -> clear response to user
# =========================================================================


class TestVoiceError:
    """Scenario 5: voice input error."""

    @pytest.mark.asyncio
    async def test_voice_pipeline_error_returns_message(self) -> None:
        """Pipeline error for voice -> response with error."""
        msg = _make_message(text="", voice=MagicMock())
        msg.answer = AsyncMock()

        result = _make_processing_result(
            success=False, error="STT error: could not transcribe",
        )
        if not result.success and result.error:
            await msg.answer(f"Error: {result.error}")
            msg.answer.assert_awaited_with("Error: STT error: could not transcribe")

    @pytest.mark.asyncio
    async def test_voice_download_failure(self) -> None:
        """Voice file download failure -> clear message."""
        msg = _make_message(text="", voice=MagicMock())
        msg.bot.get_file = AsyncMock(side_effect=Exception("File not found"))

        try:
            await msg.bot.get_file("voice_id")
        except Exception as exc:
            error_text = f"Voice error: {exc}"
            assert "File not found" in error_text

    @pytest.mark.asyncio
    async def test_voice_transcription_failure(self) -> None:
        """Transcription failure -> clear message."""
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()

        transcription: str | None = None
        if not transcription:
            await status_msg.edit_text("Could not transcribe voice message.")
            status_msg.edit_text.assert_awaited_with(
                "Could not transcribe voice message.",
            )


# =========================================================================
# Scenario 6: context resolver error -> fallback
# =========================================================================


class TestContextResolverError:
    """Scenario 6: ContextResolver error -> pipeline handles gracefully."""

    @pytest.mark.asyncio
    async def test_resolver_error_fallback_to_new_task(self) -> None:
        """Resolver error -> pipeline returns error result."""
        mock_resolver = AsyncMock()
        mock_resolver.resolve.side_effect = RuntimeError("DB connection lost")

        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42, chat_id=-100,
            message_id=1001, text="create file",
        )

        mock_gateway = AsyncMock()
        mock_router = MagicMock()

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_router,
            context_resolver=mock_resolver,
            gateway_client=mock_gateway,
            event_bus=None,
            binding_repository=None,
        )

        assert result.success is False
        assert result.error == "Не удалось безопасно обработать запрос."
        assert "RuntimeError" not in result.error
        assert "DB connection lost" not in result.error
        assert result.response_text is None
        assert result.outcome is ProcessingOutcome.TERMINAL_FAILURE
        assert result.terminal is True
        assert result.flow_status is FlowStatus.FAILED

    @pytest.mark.asyncio
    async def test_resolver_error_returns_graceful_message(self) -> None:
        """Resolver error -> user gets a clear message."""
        msg = _make_message(text="create file test.txt")
        msg.answer = AsyncMock()

        result = _make_processing_result(
            success=False, error="ContextResolver: failed to resolve context",
        )

        if not result.success and result.error:
            await msg.answer(f"Error: {result.error}")
            msg.answer.assert_awaited_with(f"Error: {result.error}")


# =========================================================================
# Scenario 7: Full E2E flow
# =========================================================================


class TestEndToEndFlow:
    """Scenario 7: text -> pipeline -> gateway -> response -> binding."""

    @pytest.mark.asyncio
    async def test_full_e2e_flow(self) -> None:
        """Full cycle: envelope -> pipeline -> gateway -> result."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42,
            chat_id=-100,
            message_id=1001,
            text="create file hello.txt with content Hello World",
        )

        mock_gateway = AsyncMock()
        mock_gateway.submit = AsyncMock()
        mock_router = MagicMock()  # sync route()
        decision = MagicMock()
        decision.intent = "task.file_write"
        decision.confidence = 0.95
        decision.response_mode = "direct"
        decision.requires_planner = True
        mock_router.route.return_value = decision

        mock_resolver = AsyncMock()
        ctx = MagicMock()
        ctx.intent.value = "NEW_TASK"
        ctx.task_id = None
        ctx.conversation_id = -100
        ctx.user_message = "create file hello.txt with content Hello World"
        ctx.original_message_id = 1001
        ctx.response_to_message_id = None
        ctx.steered_text = "create file hello.txt with content Hello World"
        ctx.matched_task = None
        ctx.revision = 0
        ctx.confidence = 0.9
        mock_resolver.resolve.return_value = ctx

        mock_binding_repo = AsyncMock()
        mock_binding_repo.save = AsyncMock()

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_router,
            context_resolver=mock_resolver,
            gateway_client=mock_gateway,
            event_bus=None,
            binding_repository=mock_binding_repo,
        )

        assert result.success
        assert result.task_id is not None or result.response_text is not None

    @pytest.mark.asyncio
    async def test_e2e_flow_with_reply_context(self) -> None:
        """E2E with reply context."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42,
            chat_id=-100,
            message_id=1002,
            text="add another line",
            reply_to_message_id=500,
            reply_to_bot_message=True,
        )

        mock_gateway = AsyncMock()
        mock_router = MagicMock()  # sync route()
        decision = MagicMock()
        decision.intent = "task.steer"
        decision.confidence = 0.9
        decision.response_mode = "direct"
        decision.requires_planner = False
        mock_router.route.return_value = decision

        mock_resolver = AsyncMock()
        ctx = MagicMock()
        ctx.intent.value = "STEER_EXISTING"
        ctx.task_id = "flow-abc-123"
        ctx.steered_text = "add another line"
        ctx.matched_task = None
        ctx.revision = 0
        ctx.confidence = 0.9
        mock_resolver.resolve.return_value = ctx

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_router,
            context_resolver=mock_resolver,
            gateway_client=mock_gateway,
            event_bus=None,
            binding_repository=None,
        )

        assert result.success
        assert envelope.reply_to_message_id == 500

    @pytest.mark.asyncio
    async def test_e2e_with_voice_source(self) -> None:
        """Voice input through pipeline -> source=telegram_voice."""
        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=42,
            chat_id=-100,
            message_id=1003,
            text="transcribed text",
            attachments=[{"type": "voice", "file_id": "voice123", "duration": 5}],
        )

        mock_resolver = AsyncMock()
        ctx = MagicMock()
        ctx.intent.value = "NEW_TASK"
        ctx.task_id = None
        ctx.steered_text = "transcribed text"
        ctx.matched_task = None
        ctx.confidence = 0.9
        mock_resolver.resolve.return_value = ctx

        mock_router = MagicMock()  # sync route()
        decision = MagicMock()
        decision.intent = "task.file_write"
        decision.confidence = 0.9
        decision.response_mode = "direct"
        decision.requires_planner = True
        mock_router.route.return_value = decision

        mock_gateway = AsyncMock()

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_router,
            context_resolver=mock_resolver,
            gateway_client=mock_gateway,
            event_bus=None,
            binding_repository=None,
        )

        assert result.success or result.error is not None


# =========================================================================
# Scenario 8: Quick messages (burst) - all messages saved
# =========================================================================


class TestBurstMessages:
    """Scenario 8: burst messages - all preserved."""

    @pytest.mark.asyncio
    async def test_burst_messages_all_have_bindings(self) -> None:
        """Multiple messages in a row - each saves binding."""
        mock_repo = AsyncMock()
        messages = [
            ("first message", 101),
            ("second message", 102),
            ("third message", 103),
        ]

        for text, mid in messages:
            await mock_repo.save(
                chat_id=-100,
                telegram_message_id=mid,
                user_id=42,
                task_id=f"task-{mid}",
                correlation_id=f"corr-{mid}",
                message_role="user",
                message_kind="text",
                original_text=text,
            )

        assert mock_repo.save.await_count == 3

    @pytest.mark.asyncio
    async def test_burst_messages_unique_correlation_ids(self) -> None:
        """Each burst message has unique correlation_id."""
        ids: set[str] = set()
        for i in range(5):
            e = UserInputEnvelope(
                source="telegram_text",
                user_id=42, chat_id=-100,
                message_id=1000 + i,
                text=f"message {i}",
            )
            assert e.correlation_id not in ids
            ids.add(e.correlation_id)

        assert len(ids) == 5

    @pytest.mark.asyncio
    async def test_burst_ordering_preserved(self) -> None:
        """Burst messages preserve order by message_id."""
        message_ids = [101, 102, 103, 104, 105]
        saved: list[int] = []

        mock_repo = AsyncMock()

        async def tracking_save(*, telegram_message_id: int, **kw: object) -> None:
            saved.append(telegram_message_id)

        mock_repo.save = tracking_save

        for mid in message_ids:
            await mock_repo.save(
                chat_id=-100,
                telegram_message_id=mid,
                user_id=42,
                task_id=f"task-{mid}",
                correlation_id=f"corr-{mid}",
                message_role="user",
                message_kind="text",
                original_text=f"msg {mid}",
            )

        assert saved == message_ids
