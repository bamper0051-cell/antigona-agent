"""Domain models for pure CLI UI presentation layer.

Clean-room presentation models: no Core/Gateway, DB, network, or external dependencies.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChatMessageRole(StrEnum):
    """Role of a chat message."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class TerminalOutcomeStatus(StrEnum):
    """Terminal outcome status codes.

    ONLY SUCCESS represents explicit validated terminal success.
    All other statuses (ACCEPTED, QUEUED, WAITING_APPROVAL, TIMEOUT, CANCELLED,
    MALFORMED, FAILED, ERROR, REJECTED) are non-success states.
    """
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ERROR = "ERROR"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    QUEUED = "QUEUED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    ACCEPTED = "ACCEPTED"
    MALFORMED = "MALFORMED"


@dataclass
class ChatMessage:
    """Ephemeral presentation model for a single chat message."""
    role: ChatMessageRole | str
    content: str
    timestamp: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TerminalOutcome:
    """Explicitly typed terminal outcome gate for presentation."""
    status: TerminalOutcomeStatus | str
    result_data: Any = None
    error_message: str | None = None

    def is_success(self) -> bool:
        """Return True ONLY for the typed ``TerminalOutcomeStatus.SUCCESS`` member.

        Success is a typed decision made upstream by a validating adapter, never a
        spelling. Raw strings (``"SUCCESS"``, ``"success"``, ``"DONE"``), ``None``,
        malformed values, and objects whose ``__str__``/``__repr__`` merely spell a
        success token all fail closed.
        """
        return isinstance(self.status, TerminalOutcomeStatus) and (
            self.status is TerminalOutcomeStatus.SUCCESS
        )


@dataclass
class ChatUIState:
    """Ephemeral UI state for pure presentation renderer."""
    messages: list[ChatMessage] = field(default_factory=list)
    current_status: str = "idle"
    is_animating: bool = False
    spinner_name: str = "dots"
    width: int | None = None
    no_color: bool = False
    terminal_outcome: TerminalOutcome | None = None
    # ── Status panel fields (real data from the Gateway, not a mock) ─────────
    gateway_url: str = ""
    session_id: str = ""
    active_flows: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    last_event: str = ""
    # ── Live agent monitor (structured, safe Gateway events) ─────────────────
    events: list[dict[str, Any]] = field(default_factory=list)
    #: connection state: "connected" | "reconnecting" | "disconnected"
    connection: str = "connected"
    active_flow_id: str = ""
    flow_started_at: float | None = None
