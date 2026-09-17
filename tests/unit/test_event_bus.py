"""DAY 9 — Tests for the typed event system: EventBus, event types,
correlation_id propagation, and cancellation.

Tests:
  - Event creation with correlation_id
  - EventBus publish/subscribe (typed)
  - correlation_id propagation across events
  - Multiple subscribers for the same event type
  - Async handler errors do not crash the bus
  - subscribe_any (wildcard) handler
  - Unsubscribe
  - Cancellation: request_cancel, cancel_event, is_cancelled
  - Cancellation: confirm_cancelled
  - Serialization: event_to_dict / event_from_dict
  - correlation_id in IntentDecision and ConversationState
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from antigona.events.bus import EventBus
from antigona.events.event_types import (
    BaseEvent,
    Cancelled,
    CancelRequested,
    ConversationReply,
    ErrorOccurred,
    IntentClassified,
    MessageReceived,
    TaskApproved,
    TaskCompleted,
    TaskCreated,
    TaskRejected,
    ToolExecuted,
    event_type_from_name,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Event creation with correlation_id
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventCreation:
    """Every typed event carries a correlation_id."""

    def test_message_received(self) -> None:
        ev = MessageReceived(
            correlation_id="corr-abc",
            chat_id=123,
            user_id=456,
            text="hello",
            message_id=1,
        )
        assert ev.correlation_id == "corr-abc"
        assert ev.chat_id == 123
        assert ev.text == "hello"

    def test_intent_classified(self) -> None:
        ev = IntentClassified(
            correlation_id="corr-def",
            text="create file",
            intent="task.file_write",
            confidence=0.95,
            response_mode="task_preview",
            reason_code="explicit_file_creation_phrase",
            entities={"path": "hello.txt"},
        )
        assert ev.correlation_id == "corr-def"
        assert ev.intent == "task.file_write"
        assert ev.confidence == 0.95

    def test_task_created(self) -> None:
        ev = TaskCreated(
            correlation_id="corr-ghi",
            task_id="flow-42",
            goal="create file hello.txt",
            flow_data={"id": "flow-42", "status": "PENDING"},
        )
        assert ev.correlation_id == "corr-ghi"
        assert ev.task_id == "flow-42"
        assert ev.flow_data["status"] == "PENDING"

    def test_task_approved(self) -> None:
        ev = TaskApproved(correlation_id="corr-jkl", task_id="flow-42", approval_id="app-1")
        assert ev.task_id == "flow-42"

    def test_task_rejected(self) -> None:
        ev = TaskRejected(correlation_id="corr-mno", task_id="flow-42", approval_id="app-1")
        assert ev.task_id == "flow-42"

    def test_task_completed(self) -> None:
        ev = TaskCompleted(
            correlation_id="corr-pqr",
            task_id="flow-42",
            status="DONE",
            result={"output": "done"},
        )
        assert ev.status == "DONE"

    def test_tool_executed(self) -> None:
        ev = ToolExecuted(
            correlation_id="corr-stu",
            task_id="flow-42",
            tool_name="workspace.write_text",
            params={"path": "hello.txt"},
            success=True,
            output="written",
        )
        assert ev.tool_name == "workspace.write_text"
        assert ev.success is True

    def test_error_occurred(self) -> None:
        ev = ErrorOccurred(
            correlation_id="corr-vwx",
            source_component="bot",
            error_type="ValueError",
            message="something went wrong",
            details={"line": 42},
        )
        assert ev.source_component == "bot"
        assert ev.error_type == "ValueError"

    def test_conversation_reply(self) -> None:
        ev = ConversationReply(
            correlation_id="corr-yz",
            chat_id=123,
            text="Hello!",
            intent="conversation.greeting",
        )
        assert ev.text == "Hello!"

    def test_cancel_requested(self) -> None:
        ev = CancelRequested(
            correlation_id="corr-c1",
            task_id="flow-42",
            reason="user cancelled",
        )
        assert ev.task_id == "flow-42"
        assert ev.reason == "user cancelled"

    def test_cancelled(self) -> None:
        ev = Cancelled(
            correlation_id="corr-c2",
            task_id="flow-42",
            reason="user cancelled",
        )
        assert ev.task_id == "flow-42"

    def test_all_event_types_have_correlation_id_field(self) -> None:
        """Every event type must have correlation_id as its first field."""
        event_classes = [
            MessageReceived,
            IntentClassified,
            TaskCreated,
            TaskApproved,
            TaskRejected,
            TaskCompleted,
            ToolExecuted,
            ErrorOccurred,
            ConversationReply,
            CancelRequested,
            Cancelled,
        ]
        for cls in event_classes:
            assert "correlation_id" in cls.__dataclass_fields__, (
                f"{cls.__name__} missing correlation_id"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: EventBus publish/subscribe
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_publish_subscribe_basic() -> None:
    """Basic publish/subscribe with typed event."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe(MessageReceived, handler)
    ev = MessageReceived(correlation_id="test-1", text="hello", chat_id=1, user_id=1, message_id=1)
    await bus.publish(ev)

    assert len(received) == 1
    assert received[0].text == "hello"
    assert received[0].correlation_id == "test-1"


@pytest.mark.asyncio
async def test_publish_subscribe_multiple_types() -> None:
    """Subscribe to different types independently."""
    bus = EventBus()
    messages: list[Any] = []
    intents: list[Any] = []

    async def msg_handler(ev: BaseEvent) -> None:
        messages.append(ev)

    async def intent_handler(ev: BaseEvent) -> None:
        intents.append(ev)

    bus.subscribe(MessageReceived, msg_handler)
    bus.subscribe(IntentClassified, intent_handler)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    await bus.publish(
        IntentClassified(correlation_id="c2", text="hi", intent="conversation.greeting",
                         confidence=0.9, response_mode="conversation", reason_code="greeting")
    )

    assert len(messages) == 1
    assert len(intents) == 1
    # Each handler should only get its own type
    assert messages[0].correlation_id == "c1"
    assert intents[0].correlation_id == "c2"


@pytest.mark.asyncio
async def test_handler_does_not_receive_wrong_type() -> None:
    """A handler subscribed to type A does NOT receive type B events."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe(IntentClassified, handler)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    assert len(received) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Multiple subscribers for same type
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_multiple_subscribers_same_type() -> None:
    """Multiple handlers for the same event type all fire."""
    bus = EventBus()
    results: list[str] = []

    async def handler_a(ev: BaseEvent) -> None:
        results.append("A")

    async def handler_b(ev: BaseEvent) -> None:
        results.append("B")

    bus.subscribe(MessageReceived, handler_a)
    bus.subscribe(MessageReceived, handler_b)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    assert sorted(results) == ["A", "B"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Handler errors do not crash the bus
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handler_error_does_not_crash_bus() -> None:
    """A crashing handler does not affect other handlers."""
    bus = EventBus()
    results: list[str] = []

    async def crashing_handler(ev: BaseEvent) -> None:
        raise ValueError("oops")

    async def good_handler(ev: BaseEvent) -> None:
        results.append("OK")

    bus.subscribe(MessageReceived, crashing_handler)
    bus.subscribe(MessageReceived, good_handler)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    assert results == ["OK"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: subscribe_any (wildcard)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_subscribe_any_catches_all_events() -> None:
    """Wildcard handler receives every published event."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def any_handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe_any(any_handler)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    await bus.publish(
        IntentClassified(correlation_id="c2", text="hi", intent="task.file_write",
                         confidence=0.9, response_mode="task_preview", reason_code="test")
    )

    assert len(received) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Unsubscribe
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unsubscribe_stops_handler() -> None:
    """After unsubscribing, the handler is not called."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe(MessageReceived, handler)
    bus.unsubscribe(MessageReceived, handler)

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    assert len(received) == 0


@pytest.mark.asyncio
async def test_unsubscribe_via_callable() -> None:
    """The callable returned by subscribe() unsubscribes the handler."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    unsubscribe = bus.subscribe(MessageReceived, handler)
    unsubscribe()

    await bus.publish(
        MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    )
    assert len(received) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: correlation_id propagation
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_correlation_id_propagation_across_events() -> None:
    """Simulate the full lifecycle: message → intent → task → reply."""
    bus = EventBus()
    events: list[BaseEvent] = []

    async def record(ev: BaseEvent) -> None:
        events.append(ev)

    # Subscribe to everything
    bus.subscribe_any(record)

    cid = "corr-lifecycle-001"
    await bus.publish(
        MessageReceived(correlation_id=cid, text="hello", chat_id=1, user_id=1, message_id=1)
    )
    await bus.publish(
        IntentClassified(correlation_id=cid, text="hello", intent="task.file_write",
                         confidence=0.95, response_mode="task_preview",
                         reason_code="test")
    )
    await bus.publish(
        TaskCreated(correlation_id=cid, task_id="flow-1", goal="hello")
    )
    await bus.publish(
        ConversationReply(correlation_id=cid, chat_id=1, text="OK", intent="task.file_write")
    )

    for ev in events:
        assert ev.correlation_id == cid, (
            f"Event {type(ev).__name__} has wrong correlation_id: "
            f"'{ev.correlation_id}' != '{cid}'"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: Cancellation
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_request_publishes_event() -> None:
    """request_cancel publishes CancelRequested."""
    bus = EventBus()
    received: list[Any] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe(CancelRequested, handler)
    await bus.request_cancel(task_id="flow-42", reason="test cancel",
                             correlation_id="corr-cancel")

    assert len(received) == 1
    assert received[0].task_id == "flow-42"
    assert received[0].reason == "test cancel"
    assert received[0].correlation_id == "corr-cancel"


@pytest.mark.asyncio
async def test_cancel_event_signals_async() -> None:
    """cancel_event can be awaited by in-flight work."""
    bus = EventBus()
    task_id = "flow-42"

    cancel_ev = bus.register_cancel_event(task_id)
    assert cancel_ev is not None
    assert not cancel_ev.is_set()

    # Request cancellation in background
    async def do_cancel() -> None:
        await asyncio.sleep(0.01)
        await bus.request_cancel(task_id=task_id, reason="timeout")

    async def do_work() -> None:
        # Simulate waiting for cancellation
        try:
            await asyncio.wait_for(cancel_ev.wait(), timeout=1.0)
        except TimeoutError:
            pass  # no cancellation within timeout — fine

    await asyncio.gather(do_cancel(), do_work())
    assert cancel_ev.is_set()


@pytest.mark.asyncio
async def test_is_cancelled() -> None:
    """is_cancelled returns True after confirm_cancelled."""
    bus = EventBus()
    assert not bus.is_cancelled("flow-42")

    await bus.confirm_cancelled(task_id="flow-42", reason="done",
                                correlation_id="corr-c1")
    assert bus.is_cancelled("flow-42")


@pytest.mark.asyncio
async def test_confirm_cancelled_publishes_cancelled_event() -> None:
    """confirm_cancelled publishes Cancelled event."""
    bus = EventBus()
    received: list[Any] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe(Cancelled, handler)
    await bus.confirm_cancelled(task_id="flow-42", reason="done",
                                correlation_id="corr-c1")

    assert len(received) == 1
    assert received[0].task_id == "flow-42"
    assert received[0].reason == "done"


@pytest.mark.asyncio
async def test_cancel_request_publishes_to_subscribers() -> None:
    """Wildcard subscribers also receive cancellation events."""
    bus = EventBus()
    all_events: list[BaseEvent] = []

    async def any_handler(ev: BaseEvent) -> None:
        all_events.append(ev)

    bus.subscribe_any(any_handler)
    await bus.request_cancel(task_id="flow-99", reason="user abort",
                             correlation_id="corr-abort")

    assert len(all_events) >= 1
    cancel_event = all_events[0]
    assert isinstance(cancel_event, CancelRequested)
    assert cancel_event.task_id == "flow-99"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Serialization — event_to_dict / event_from_dict
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventSerialization:
    """Round-trip serialization for all event types."""

    @pytest.mark.parametrize(
        "event",
        [
            MessageReceived(correlation_id="c1", chat_id=1, user_id=1, text="hi", message_id=1),
            IntentClassified(correlation_id="c2", text="hi", intent="task.file_write",
                             confidence=0.9, response_mode="task_preview",
                             reason_code="test"),
            TaskCreated(correlation_id="c3", task_id="flow-1", goal="test"),
            TaskApproved(correlation_id="c4", task_id="flow-1", approval_id="app-1"),
            TaskRejected(correlation_id="c5", task_id="flow-1", approval_id="app-1"),
            TaskCompleted(correlation_id="c6", task_id="flow-1", status="DONE"),
            ToolExecuted(correlation_id="c7", task_id="flow-1", tool_name="test",
                         params={}, success=True),
            ErrorOccurred(correlation_id="c8", source_component="test",
                          error_type="ValueError", message="oops"),
            ConversationReply(correlation_id="c9", chat_id=1, text="hello",
                              intent="task.file_write"),
            CancelRequested(correlation_id="c10", task_id="flow-1", reason="cancel"),
            Cancelled(correlation_id="c11", task_id="flow-1", reason="done"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_round_trip(self, event: BaseEvent) -> None:
        data = EventBus.event_to_dict(event)
        restored = EventBus.event_from_dict(data)

        assert restored is not None, f"Failed to restore {type(event).__name__}"
        assert isinstance(restored, type(event)), (
            f"Type mismatch: {type(restored).__name__} != {type(event).__name__}"
        )
        assert restored.correlation_id == event.correlation_id
        # Check a type-specific field for each event
        if isinstance(restored, MessageReceived):
            assert restored.text == event.text  # type: ignore[comparison-overlap]
        elif isinstance(restored, IntentClassified):
            assert restored.intent == event.intent  # type: ignore[comparison-overlap]
        elif isinstance(restored, CancelRequested):
            assert restored.task_id == event.task_id  # type: ignore[comparison-overlap]

    def test_unknown_type_returns_none(self) -> None:
        data = {"_type": "UnknownEvent", "correlation_id": "x"}
        assert EventBus.event_from_dict(data) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 10: event_type_from_name lookup
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventTypeFromName:
    def test_all_known_types(self) -> None:
        names = [
            "MessageReceived", "IntentClassified", "TaskCreated",
            "TaskApproved", "TaskRejected", "TaskCompleted", "ToolExecuted",
            "ErrorOccurred", "ConversationReply", "CancelRequested", "Cancelled",
        ]
        for name in names:
            cls = event_type_from_name(name)
            assert cls is not None, f"Unknown type name: {name}"
            assert cls.__name__ == name

    def test_unknown_returns_none(self) -> None:
        assert event_type_from_name("NonExistent") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 11: EventBus timestamp auto-set on publish
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_timestamp_set_on_publish() -> None:
    """Events with timestamp=0 get an auto timestamp on publish."""
    bus = EventBus()
    received: list[BaseEvent] = []

    async def handler(ev: BaseEvent) -> None:
        received.append(ev)

    bus.subscribe_any(handler)

    ev = MessageReceived(correlation_id="c1", text="hi", chat_id=1, user_id=1, message_id=1)
    assert ev.timestamp == 0.0

    await bus.publish(ev)
    assert len(received) == 1
    assert received[0].timestamp > 0.0
