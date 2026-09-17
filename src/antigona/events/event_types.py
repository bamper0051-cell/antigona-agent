"""Typed event classes for the Antigona event system.

Every event carries a ``correlation_id`` that links it across the entire
message-to-response lifecycle. Additional typed fields carry event-specific
payload so consumers never need to parse raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "BaseEvent",
    "TaskSteeringEvent",
    "MessageReceived",
    "IntentClassified",
    "TaskCreated",
    "TaskApproved",
    "TaskRejected",
    "TaskCompleted",
    "ToolExecuted",
    "ErrorOccurred",
    "ConversationReply",
    "CancelRequested",
    "Cancelled",
    # ── Operation-level events ──
    "OperationReceived",
    "StageChanged",
    "ToolProgress",
    "FinalResponseReady",
    "FinalResponseSent",
    "DeliveryConfirmed",
    "RecoveryTriggered",
]


@dataclass
class BaseEvent:
    """Base for all typed events.

    Every subclass inherits *correlation_id*, *timestamp*, and *source*.
    """

    correlation_id: str = ""
    timestamp: float = 0.0
    source: str = ""


# ── Lifecycle events ─────────────────────────────────────────────────────────


@dataclass
class MessageReceived(BaseEvent):
    """Published when a raw user message arrives at the transport layer."""

    chat_id: int = 0
    user_id: int = 0
    text: str = ""
    message_id: int = 0


@dataclass
class IntentClassified(BaseEvent):
    """Published after the IntentRouter classifies a message."""

    text: str = ""
    intent: str = ""
    confidence: float = 0.0
    response_mode: str = ""
    reason_code: str = ""
    entities: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskCreated(BaseEvent):
    """Published when a new task flow is created on the Gateway."""

    task_id: str = ""
    goal: str = ""
    flow_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskApproved(BaseEvent):
    """Published when a task receives user approval."""

    task_id: str = ""
    approval_id: str = ""


@dataclass
class TaskRejected(BaseEvent):
    """Published when a task is rejected by the user."""

    task_id: str = ""
    approval_id: str = ""


@dataclass
class TaskCompleted(BaseEvent):
    """Published when a task reaches a terminal state (DONE / FAILED / etc.)."""

    task_id: str = ""
    status: str = ""
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecuted(BaseEvent):
    """Published after a tool/action has been executed."""

    task_id: str = ""
    tool_name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    output: Any = None


@dataclass
class ErrorOccurred(BaseEvent):
    """Published when an error happens anywhere in the pipeline."""

    source_component: str = ""
    error_type: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationReply(BaseEvent):
    """Published when the bot sends a reply to the user."""

    chat_id: int = 0
    text: str = ""
    intent: str = ""


# ── Cancellation events ──────────────────────────────────────────────────────


@dataclass
class CancelRequested(BaseEvent):
    """Published when cancellation is requested for a task.

    Subscribers (e.g. executor, streamer) should check this and abort work.
    """

    task_id: str = ""
    reason: str = ""


@dataclass
class Cancelled(BaseEvent):
    """Published after a task has been successfully cancelled."""

    task_id: str = ""
    reason: str = ""


# ── Operation-level events ────────────────────────────────────────────────────


@dataclass
class OperationReceived(BaseEvent):
    """Published when a new operation request arrives from the transport layer."""

    operation_id: str = ""
    chat_id: int = 0
    user_id: int = 0
    text: str = ""
    message_id: int = 0


@dataclass
class StageChanged(BaseEvent):
    """Published when an operation advances to a new stage."""

    operation_id: str = ""
    stage: str = ""
    step: int = 0
    total: int = 0
    tool_name: str | None = None
    description: str | None = None


@dataclass
class ToolProgress(BaseEvent):
    """Published to report progress or completion of a single tool call."""

    operation_id: str = ""
    tool_name: str = ""
    status: str = ""
    exit_code: int | None = None
    stdout_preview: str | None = None


@dataclass
class FinalResponseReady(BaseEvent):
    """Final payload plus the explicitly intended operation terminal state."""

    operation_id: str = ""
    text: str = ""
    parse_mode: str = "HTML"
    # Empty is deliberately fail-closed: every producer must name the
    # authoritative outcome instead of accidentally defaulting failures to
    # success.
    terminal_state: str = ""
    #: Filesystem paths the core wants delivered alongside the final text.
    #: They are *requests*: the presenter revalidates every one of them against
    #: its allow-list before a single byte is sent.
    artifacts: tuple[str, ...] = ()
    #: True only when the turn actually completed a real action (a task flow
    #: reached a confirmed terminal state, a tool ran successfully, or a real
    #: artifact was delivered).  Defaults to False so a plain conversational
    #: reply never renders a "✅ Готово" completion header.
    completed_action: bool = False


@dataclass
class FinalResponseSent(BaseEvent):
    """Published after the final response has been sent to the transport."""

    operation_id: str = ""
    final_message_ids: list[int] = field(default_factory=list)


@dataclass
class DeliveryConfirmed(BaseEvent):
    """Published when the transport confirms a message was delivered."""

    operation_id: str = ""
    final_message_id: int = 0
    terminal_state: str = ""


@dataclass
class TaskSteeringEvent(BaseEvent):
    """Published when a running task is steered by user edit / command."""

    task_id: str = ""
    source: str = ""
    message_id: int = 0
    old_text: str = ""
    new_text: str = ""
    changed_at: datetime | None = None


@dataclass
class RecoveryTriggered(BaseEvent):
    """Published when the system automatically triggers a recovery action
    in response to an error."""

    operation_id: str = ""
    error: str = ""
    recovery_action: str = ""


# ── Event registry for dispatch ──────────────────────────────────────────────

_EVENT_TYPE_MAP: dict[str, type[BaseEvent]] = {
    "MessageReceived": MessageReceived,
    "TaskSteeringEvent": TaskSteeringEvent,
    "IntentClassified": IntentClassified,
    "TaskCreated": TaskCreated,
    "TaskApproved": TaskApproved,
    "TaskRejected": TaskRejected,
    "TaskCompleted": TaskCompleted,
    "ToolExecuted": ToolExecuted,
    "ErrorOccurred": ErrorOccurred,
    "ConversationReply": ConversationReply,
    "CancelRequested": CancelRequested,
    "Cancelled": Cancelled,
    # ── Operation-level events ──
    "OperationReceived": OperationReceived,
    "StageChanged": StageChanged,
    "ToolProgress": ToolProgress,
    "FinalResponseReady": FinalResponseReady,
    "FinalResponseSent": FinalResponseSent,
    "DeliveryConfirmed": DeliveryConfirmed,
    "RecoveryTriggered": RecoveryTriggered,
}


def event_type_from_name(name: str) -> type[BaseEvent] | None:
    """Look up an event class by its ``__name__`` string."""
    return _EVENT_TYPE_MAP.get(name)
