"""Input pipeline data models — envelope and processing result.

UserInputEnvelope is the canonical input contract for every channel.
ProcessingResult is the canonical output contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from antigona.core.control_plane import FlowStatus


@dataclass(slots=True)
class UserInputEnvelope:
    """Normalised envelope for any user input, regardless of source channel.

    Attributes:
        source: Source channel identifier, e.g. ``"telegram_text"``,
            ``"telegram_voice"``, ``"telegram_edited"``, ``"cli"``.
        user_id: Platform-specific user ID.
        chat_id: Platform-specific conversation/chat ID.
        message_id: Platform-specific message ID.
        text: Normalised text of the message. For voice — transcription.
        reply_to_message_id: ID of the message being replied to (if any).
        reply_to_bot_message: Whether the reply targets a bot-owned message.
        edited_message_id: If this is an edit — the original message ID.
        attachments:  List of attachment metadata (file IDs, URLs, etc.).
        timestamp: When the message was originally sent (UTC).
        metadata:  Free-form metadata for channel-specific extras.
        correlation_id:  End-to-end trace ID. Auto-generated if empty.
    """

    source: str  # "telegram_text" | "telegram_voice" | "telegram_edited" | "cli"
    user_id: int
    chat_id: int
    message_id: int
    text: str
    reply_to_message_id: int | None = None
    reply_to_bot_message: bool = False
    edited_message_id: int | None = None
    attachments: list[Any] | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""

    def __post_init__(self) -> None:
        import uuid

        if not self.correlation_id:
            self.correlation_id = uuid.uuid4().hex
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


class ProcessingOutcome(StrEnum):
    """Typed semantic outcome of one pipeline call.

    ``success`` on :class:`ProcessingResult` only describes whether the
    pipeline/API call itself completed.  This enum describes whether the
    underlying task is still active or reached an authoritative terminal
    outcome.
    """

    CONVERSATION_FINAL = "CONVERSATION_FINAL"
    FLOW_ACCEPTED = "FLOW_ACCEPTED"
    FLOW_STEERED = "FLOW_STEERED"
    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class ProcessingResult:
    """Result after a user input has been processed by the full pipeline.

    Attributes:
        success: Whether the pipeline completed without an error.
        task_id: The flow / task ID returned by Gateway (if created/resolved).
        session_id: Session identifier (if a conversation session exists).
        response_text: Human-readable response to send back to the user.
        error: Error message if the pipeline failed.
        correlation_id: End-to-end trace ID, matches the envelope.
        duration_ms: Total wall-clock time the pipeline took (ms).
        outcome: Typed semantic outcome. The fail-closed default is a
            nonterminal accepted flow for backwards-compatible call sites.
        terminal: Whether an authoritative terminal state was observed.
        flow_status: Raw authoritative Gateway status, when available.
    """

    success: bool
    task_id: str | None
    session_id: str | None
    response_text: str | None
    error: str | None
    correlation_id: str
    duration_ms: float
    outcome: ProcessingOutcome = ProcessingOutcome.FLOW_ACCEPTED
    terminal: bool = False
    flow_status: FlowStatus | str | None = None


__all__ = [
    "ProcessingOutcome",
    "ProcessingResult",
    "UserInputEnvelope",
]
