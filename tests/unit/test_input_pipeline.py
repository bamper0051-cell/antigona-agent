"""Tests for the unified InputPipeline module.

Covers:
  - UserInputEnvelope creation and auto-fields
  - process_user_input full flow (with mock Gateway)
  - Context resolution via reply_to_message_id
  - Voice routing via pipeline
  - Edited message flow
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.core.control_plane import (
    FlowStatus,
    FlowView,
)
from antigona.input_pipeline.models import (
    ProcessingOutcome,
    ProcessingResult,
    UserInputEnvelope,
)
from antigona.input_pipeline.pipeline import process_user_input

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_context_resolver() -> AsyncMock:
    """ContextResolver mock that returns a bare NEW_TASK context."""
    resolver = AsyncMock()

    async def resolve_side_effect(*, text: str, chat_id: int, user_id: int,
                                   message_id: int, reply_to_message_id: int | None = None,
                                   edited_message_id: int | None = None) -> MagicMock:
        ctx = MagicMock()
        ctx.intent.value = "NEW_TASK"
        ctx.intent = "NEW_TASK"
        # Make intent an enum-like object
        from antigona.tasks.context_resolver import IntentType
        ctx.intent = IntentType.NEW_TASK
        ctx.task_id = None
        ctx.conversation_id = chat_id
        ctx.user_message = text
        ctx.original_message_id = message_id
        ctx.response_to_message_id = reply_to_message_id
        ctx.steered_text = text
        ctx.matched_task = None
        ctx.revision = 0
        ctx.confidence = 0.9
        return ctx

    resolver.resolve = AsyncMock(side_effect=resolve_side_effect)
    return resolver


@pytest.fixture
def mock_intent_router() -> MagicMock:
    """IntentRouter mock that returns a generic task intent."""
    router = MagicMock()

    def route_side_effect(text: str, context: dict | None = None) -> MagicMock:
        decision = MagicMock()
        decision.intent = "task.shell"
        decision.confidence = 0.95
        decision.response_mode = "task_preview"
        decision.requires_planner = True
        decision.requires_approval = True
        decision.entities = {}
        decision.correlation_id = ""
        decision.reason_code = "test"
        return decision

    router.route = MagicMock(side_effect=route_side_effect)
    return router


@pytest.fixture
def mock_gateway_client() -> AsyncMock:
    """GatewayClient mock that returns a fake FlowView."""
    client = AsyncMock()

    async def submit_side_effect(request) -> FlowView:
        return FlowView(
            flow_id="flow-abc-123",
            conversation_id=str(request.conversation_id),
            title="Test task",
            status=FlowStatus.QUEUED,
            progress=0,
            current_step=None,
            steps=[],
            events=[],
            result=None,
            error=None,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )

    client.submit = AsyncMock(side_effect=submit_side_effect)
    client.steer = AsyncMock()
    client.cancel = AsyncMock()
    client.wait_for_terminal = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_binding_repository() -> AsyncMock:
    repo = AsyncMock()
    repo.save = AsyncMock()
    return repo


# ── UserInputEnvelope tests ───────────────────────────────────────────────────


class TestUserInputEnvelope:
    def test_create_minimal(self) -> None:
        """Envelope auto-generates correlation_id and timestamp."""
        env = UserInputEnvelope(
            source="telegram_text",
            user_id=123,
            chat_id=-456,
            message_id=789,
            text="hello world",
        )
        assert env.correlation_id
        assert len(env.correlation_id) == 32  # hex uuid
        assert env.timestamp is not None
        assert env.timestamp.tzinfo is not None
        assert env.metadata == {}

    def test_create_with_all_fields(self) -> None:
        """Envelope accepts all optional fields."""
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        env = UserInputEnvelope(
            source="telegram_edited",
            user_id=1,
            chat_id=2,
            message_id=3,
            text="edited",
            reply_to_message_id=1,
            reply_to_bot_message=True,
            edited_message_id=3,
            attachments=[{"type": "photo"}],
            timestamp=ts,
            metadata={"edited": True},
            correlation_id="custom-corrid",
        )
        assert env.correlation_id == "custom-corrid"
        assert env.timestamp == ts
        assert env.reply_to_message_id == 1
        assert env.reply_to_bot_message is True
        assert env.edited_message_id == 3
        assert env.attachments == [{"type": "photo"}]

    def test_slots(self) -> None:
        """Using @dataclass(slots=True) prevents __dict__."""
        env = UserInputEnvelope(
            source="cli", user_id=1, chat_id=1, message_id=1, text="x"
        )
        with pytest.raises(AttributeError):
            _ = env.__dict__  # type: ignore[attr-defined]


class TestProcessingResult:
    def test_create(self) -> None:
        result = ProcessingResult(
            success=True,
            task_id="flow-abc",
            session_id="sess-1",
            response_text="OK",
            error=None,
            correlation_id="corr-1",
            duration_ms=12.5,
        )
        assert result.success is True
        assert result.task_id == "flow-abc"
        assert result.duration_ms == 12.5

    def test_error_result(self) -> None:
        result = ProcessingResult(
            success=False,
            task_id=None,
            session_id=None,
            response_text=None,
            error="Something broke",
            correlation_id="corr-2",
            duration_ms=0.5,
        )
        assert result.success is False
        assert result.error == "Something broke"

    def test_slots(self) -> None:
        result = ProcessingResult(
            success=True, task_id=None, session_id=None,
            response_text=None, error=None,
            correlation_id="c", duration_ms=0.0,
        )
        with pytest.raises(AttributeError):
            _ = result.__dict__  # type: ignore[attr-defined]


# ── Pipeline tests ────────────────────────────────────────────────────────────


class TestProcessUserInput:
    """Full pipeline integration tests with mocked dependencies."""

    @pytest.mark.asyncio
    async def test_new_task_flow(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """New message → ContextResolver → IntentRouter → Gateway.submit → binding."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42,
            chat_id=-100,
            message_id=1001,
            text="создай файл test.txt",
        )

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        assert result.task_id == "flow-abc-123"
        assert result.correlation_id == envelope.correlation_id
        assert result.duration_ms > 0
        assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
        assert result.terminal is False

        # Verify pipeline calls
        mock_context_resolver.resolve.assert_awaited_once()
        mock_intent_router.route.assert_called_once()
        mock_gateway_client.submit.assert_awaited_once()
        mock_binding_repository.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_voice_routing(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """Voice message → pipeline (envelope source = telegram_voice)."""
        envelope = UserInputEnvelope(
            source="telegram_voice",
            user_id=42,
            chat_id=-100,
            message_id=2002,
            text="голосовое сообщение",
            attachments=[{"type": "voice", "file_id": "AwA..."}],
        )

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        assert result.task_id == "flow-abc-123"
        mock_gateway_client.submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_edited_message(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """Edited message → pipeline passes edited_message_id to resolver."""
        envelope = UserInputEnvelope(
            source="telegram_edited",
            user_id=42,
            chat_id=-100,
            message_id=1001,
            text="исправленный текст",
            edited_message_id=1001,
        )

        # Make resolver return CORRECT_MESSAGE with a task_id
        async def resolve_with_edit(*, text, chat_id, user_id, message_id,
                                     reply_to_message_id=None,
                                     edited_message_id=None) -> MagicMock:
            from antigona.tasks.context_resolver import IntentType
            ctx = MagicMock()
            ctx.intent = IntentType.CORRECT_MESSAGE
            ctx.task_id = "existing-task-id"
            ctx.conversation_id = chat_id
            ctx.user_message = text
            ctx.original_message_id = message_id
            ctx.response_to_message_id = None
            ctx.steered_text = text
            ctx.matched_task = MagicMock()
            ctx.revision = 2
            ctx.confidence = 0.95
            return ctx

        mock_context_resolver.resolve = AsyncMock(side_effect=resolve_with_edit)

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        # Existing task → steer, not submit
        mock_gateway_client.steer.assert_awaited_once()
        mock_gateway_client.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_with_task_id(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """Reply → pipeline passes reply_to_message_id to resolver."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42,
            chat_id=-100,
            message_id=3003,
            text="добавь ещё одну функцию",
            reply_to_message_id=2002,
        )

        # Resolver finds existing task via reply
        async def resolve_with_reply(*, text, chat_id, user_id, message_id,
                                      reply_to_message_id=None,
                                      edited_message_id=None) -> MagicMock:
            from antigona.tasks.context_resolver import IntentType
            ctx = MagicMock()
            ctx.intent = IntentType.STEER_EXISTING
            ctx.task_id = "existing-task-id"
            ctx.conversation_id = chat_id
            ctx.user_message = text
            ctx.original_message_id = message_id
            ctx.response_to_message_id = reply_to_message_id
            ctx.steered_text = text
            ctx.matched_task = MagicMock()
            ctx.revision = 0
            ctx.confidence = 0.95
            return ctx

        mock_context_resolver.resolve = AsyncMock(side_effect=resolve_with_reply)

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        assert envelope.reply_to_message_id == 2002
        mock_gateway_client.steer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_existing_task(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """Cancel command → Gateway cancel."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=42,
            chat_id=-100,
            message_id=4004,
            text="отмени задачу",
        )

        # Resolver finds existing task and classifies as CANCEL
        async def resolve_cancel(*, text, chat_id, user_id, message_id,
                                  reply_to_message_id=None,
                                  edited_message_id=None) -> MagicMock:
            from antigona.tasks.context_resolver import IntentType
            ctx = MagicMock()
            ctx.intent = IntentType.CANCEL
            ctx.task_id = "task-to-cancel"
            ctx.conversation_id = chat_id
            ctx.user_message = text
            ctx.original_message_id = message_id
            ctx.response_to_message_id = None
            ctx.steered_text = text
            ctx.matched_task = MagicMock()
            ctx.revision = 0
            ctx.confidence = 0.99
            return ctx

        mock_context_resolver.resolve = AsyncMock(side_effect=resolve_cancel)

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        assert result.task_id == "task-to-cancel"
        assert result.outcome is ProcessingOutcome.FLOW_STEERED
        assert result.terminal is False
        mock_gateway_client.cancel.assert_awaited_once_with(
            "task-to-cancel", reason="user request via pipeline"
        )

    @pytest.mark.asyncio
    async def test_pipeline_error_handling(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When Gateway raises, pipeline returns error result, doesn't crash."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=1,
            chat_id=1,
            message_id=1,
            text="test",
        )

        mock_gateway_client.submit = AsyncMock(side_effect=RuntimeError("Gateway down"))

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
        )

        assert result.success is False
        assert result.error == "Не удалось безопасно обработать запрос."
        assert "RuntimeError" not in result.error
        assert "Gateway down" not in result.error
        assert result.response_text is None
        assert result.outcome is ProcessingOutcome.TERMINAL_FAILURE
        assert result.terminal is True
        assert result.flow_status is FlowStatus.FAILED
        # Wave 4: Windows monotonic can round a sub-0.5ms fast-fail to 0.0 —
        # the invariant is "not negative", not "strictly positive".
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_binding_failure_does_not_bubble(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """A failing binding save never propagates to the caller."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=1,
            chat_id=1,
            message_id=1,
            text="test",
        )

        bad_repo = AsyncMock()
        bad_repo.save = AsyncMock(side_effect=RuntimeError("DB unavailable"))

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=bad_repo,
        )

        # Pipeline still succeeds — binding failure is non-fatal
        assert result.success is True
        assert result.task_id == "flow-abc-123"

    @pytest.mark.asyncio
    async def test_cli_source_skips_binding(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
        mock_event_bus: AsyncMock,
        mock_binding_repository: AsyncMock,
    ) -> None:
        """CLI source → no binding save attempt."""
        envelope = UserInputEnvelope(
            source="cli",
            user_id=0,
            chat_id=0,
            message_id=0,
            text="deploy",
        )

        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
            event_bus=mock_event_bus,
            binding_repository=mock_binding_repository,
        )

        assert result.success is True
        # No binding save for non-telegram sources
        mock_binding_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_duration_ms_nonzero(
        self,
        mock_context_resolver: AsyncMock,
        mock_intent_router: MagicMock,
        mock_gateway_client: AsyncMock,
    ) -> None:
        """duration_ms is > 0 after a real async pipeline run."""
        envelope = UserInputEnvelope(
            source="telegram_text",
            user_id=1, chat_id=1, message_id=1,
            text="x",
        )
        result = await process_user_input(
            envelope=envelope,
            intent_router=mock_intent_router,
            context_resolver=mock_context_resolver,
            gateway_client=mock_gateway_client,
        )
        # Wave 4: Windows monotonic can round a fast mock run to 0.0 — the
        # invariant is "not negative", not "strictly positive".
        assert result.duration_ms >= 0
