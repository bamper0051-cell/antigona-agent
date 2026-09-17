"""Operation model for progress-message tracking with a full state machine.

This module provides:

* :class:`OperationState` — the enum of every state a progress operation
  may occupy during its lifecycle.
* :class:`Operation` — the SQLAlchemy ORM model persisted to the
  ``operations`` table, carrying Telegram message metadata and progress
  counters alongside the current state.
* :class:`StateMachine` — a pure-Python state machine that encodes legal
  state transitions, terminal state detection, and guarded transition
  checks with an explicit transition graph.

Every state mutation in the codebase that touches a progress operation is
expected to pass through :meth:`StateMachine.transition` before it writes
to the database, ensuring the lifecycle invariant is always enforced at
the application layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import VARCHAR, BigInteger, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base

# ── State enum ─────────────────────────────────────────────────────────────────


class OperationState(StrEnum):
    """Every state a progress operation may occupy.

    The lifecycle follows this general flow::

        RECEIVED → CLASSIFYING → PLANNING → RUNNING ⇄ WAITING_TOOL
                    ↓                           ↓
               WAITING_USER                VALIDATING → FINALIZING → SUCCEEDED
                    ↓                           ↓            ↓
                (return)                   ERROR_RECOVERY  FAILED
                    ↓                           ↓
                (any pre-terminal)       RUNNING / WAITING_USER / FAILED

    Terminal absorbing states:
        ``SUCCEEDED``, ``FAILED``, ``CANCELLED``

    See :class:`StateMachine` for the full transition graph.
    """

    RECEIVED = "RECEIVED"
    CLASSIFYING = "CLASSIFYING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    VALIDATING = "VALIDATING"
    FINALIZING = "FINALIZING"
    WAITING_USER = "WAITING_USER"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ── ORM model ──────────────────────────────────────────────────────────────────


class Operation(Base):
    """Persistent progress operation tied to a user-originating message.

    Each row tracks one operation from the moment the user's message is
    received (``RECEIVED``) through intent classification, planning,
    tool execution, and finalisation, right up to a terminal state
    (``SUCCEEDED``, ``FAILED``, or ``CANCELLED``).

    Telegram-specific fields (``chat_id``, ``origin_message_id``,
    ``progress_message_id``, etc.) allow the presenter layer to look up
    and update the correct progress bubble in the conversation.

    Attributes:
        id: UUID primary key.
        chat_id: Telegram chat (conversation) ID.  Indexed for fast
            per-chat lookups.
        user_id: Telegram user who originated the request, or *None* for
            system‑initiated operations.
        origin_message_id: Telegram message ID of the user's original
            request message.
        progress_message_id: Telegram message ID of the progress /
            status bubble, updated in-place as the operation advances.
            *None* until the first progress message is sent.
        reply_to_message_id: Optional Telegram message ID this operation
            is replying to.
        intent: Classified intent label (e.g. ``code_review``,
            ``file_edit``).  *None* until classification completes.
        flow_id: Gateway flow / task ID if a task flow was created for
            this operation.  *None* otherwise.
        tool_call_id: Identifier for the current tool invocation, if
            the operation is awaiting a tool result.
        status: Current :class:`OperationState` value.  Defaults to
            ``RECEIVED``.
        current_stage: Human-readable label for the current pipeline
            stage (e.g. ``analysing code``, ``running tests``).
            *None* when no stage-specific label is set.
        current_step: Zero-based index of the current sub-step within
            the current stage.
        total_steps: Total number of sub-steps expected in the current
            stage (best-effort estimate).
        text: The original free‑form text of the user's request.
        final_message_ids: JSON array of Telegram message IDs that
            constitute the final response.  Serves as a delivery
            manifest so the presenter knows which messages to pin,
            delete, or mark as terminal.
        last_error: Most recent error message, if the operation is in
            ``ERROR_RECOVERY`` or ``FAILED``.  *None* otherwise.
        created_at: Row creation timestamp (UTC, timezone-aware).
        updated_at: Row last-modified timestamp (UTC, timezone-aware).
    """

    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(
        VARCHAR(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    progress_message_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    reply_to_message_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    flow_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=OperationState.RECEIVED.value,
        index=True,
    )
    current_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_message_ids: Mapped[list[int]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    executed_tools: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


# ── Operation state machine ────────────────────────────────────────────────────

#: States that may not transition to any other state.
_TERMINAL_OPERATION_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
    }
)

#: Legal operation transitions.  Every key that appears here has an entry,
#: and the :class:`StateMachine` rejects anything that is not listed.
_OPERATION_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    # ── Main pipeline ──────────────────────────────────────────────────
    OperationState.RECEIVED: frozenset(
        {
            OperationState.CLASSIFYING,
            # Delivery must remain possible even when the initial progress
            # event was not observed (for example during startup recovery).
            OperationState.FINALIZING,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.CLASSIFYING: frozenset(
        {
            OperationState.PLANNING,
            OperationState.RUNNING,
            OperationState.FINALIZING,
            OperationState.WAITING_USER,
            OperationState.ERROR_RECOVERY,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.PLANNING: frozenset(
        {
            OperationState.RUNNING,
            OperationState.FINALIZING,
            OperationState.WAITING_USER,
            OperationState.ERROR_RECOVERY,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.WAITING_TOOL,
            OperationState.VALIDATING,
            OperationState.FINALIZING,
            OperationState.WAITING_USER,
            OperationState.ERROR_RECOVERY,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.WAITING_TOOL: frozenset(
        {
            OperationState.RUNNING,
            OperationState.VALIDATING,
            OperationState.FINALIZING,
            OperationState.ERROR_RECOVERY,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.VALIDATING: frozenset(
        {
            OperationState.FINALIZING,
            OperationState.ERROR_RECOVERY,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.FINALIZING: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    # ── User-interaction branch ────────────────────────────────────────
    OperationState.WAITING_USER: frozenset(
        {
            OperationState.CLASSIFYING,
            OperationState.PLANNING,
            OperationState.RUNNING,
            OperationState.FINALIZING,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    # ── Error-recovery branch ──────────────────────────────────────────
    OperationState.ERROR_RECOVERY: frozenset(
        {
            OperationState.RUNNING,
            OperationState.WAITING_USER,
            OperationState.FINALIZING,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
}


class StateMachine:
    """Pure state machine for :class:`OperationState` transitions.

    Encodes the legal edges of the operation lifecycle graph, provides
    a guard method for checking arbitrary transitions, and surfaces
    terminal-state queries.

    Usage::

        sm = StateMachine()
        sm.can_transition(OperationState.RECEIVED, OperationState.CLASSIFYING)
        # → True

        sm.transition(OperationState.SUCCEEDED, OperationState.FAILED)
        # → False  (SUCCEEDED is terminal)

        sm.is_terminal(OperationState.CANCELLED)
        # → True
    """

    @staticmethod
    def is_terminal(state: OperationState) -> bool:
        """Return *True* when *state* is an absorbing terminal.

        Terminal states (``SUCCEEDED``, ``FAILED``, ``CANCELLED``) may
        never transition to any other state.
        """
        return state in _TERMINAL_OPERATION_STATES

    @staticmethod
    def can_transition(
        from_state: OperationState, to_state: OperationState
    ) -> bool:
        """Return *True* when ``from_state -> to_state`` is a legal edge.

        Always returns *False* when *from_state* is terminal or when no
        edge exists in the transition graph.
        """
        if from_state in _TERMINAL_OPERATION_STATES:
            return False
        targets = _OPERATION_TRANSITIONS.get(from_state)
        if targets is None:
            return False
        return to_state in targets

    @staticmethod
    def transition(
        from_state: OperationState, to_state: OperationState
    ) -> bool:
        """Attempt ``from_state -> to_state`` and return whether it succeeded.

        Unlike :meth:`can_transition` this **does not raise** — it
        returns *False* on illegal transitions, making it suitable for
        optimistic usage where the caller simply wants to know if the
        move was accepted.

        .. note::

           For stricter enforcement, call :meth:`can_transition` and
           raise an :class:`InvalidTransition` yourself when it returns
           *False*.
        """
        if not StateMachine.can_transition(from_state, to_state):
            return False
        return True


# ── Convenience re-export ──────────────────────────────────────────────────────

__all__ = [
    "Operation",
    "OperationState",
    "StateMachine",
]
