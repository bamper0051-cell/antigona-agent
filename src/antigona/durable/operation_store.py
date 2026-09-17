"""Async repository for the :class:`Operation` model.

Provides CRUD and query methods for progress operations, all using the
project's :class:`Database` for session management.  Every public method
is async and wraps the underlying sync SQLAlchemy session via
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..database import Database
from .operation_models import Operation, OperationState, StateMachine

T = TypeVar("T")


class OperationNotFound(LookupError):
    """Raised when an operation ID does not match a persisted row."""


_PROGRESS_DELIVERY_CLAIM = -1
_FINAL_DELIVERY_CLAIM_PREFIX = "FINAL_DELIVERY_CLAIMED:"
_PROGRESS_CLEANUP_CLAIM = "FINAL_PROGRESS_CLEANUP_CLAIMED"
_TERMINAL_STATUS_VALUES = tuple(
    state.value for state in OperationState if StateMachine.is_terminal(state)
)


@dataclass(frozen=True, slots=True)
class FinalDeliveryClaim:
    """Durable result of trying to claim one Telegram final delivery."""

    claimed: bool
    token: str | None = None
    existing_message_id: int | None = None
    in_flight: bool = False


class OperationStore:
    """Async repository for :class:`Operation` persistence.

    Accepts the project's :class:`Database` and manages its own sessions.
    All public methods are async; the underlying sync SQLAlchemy calls run
    in a thread pool executor.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _with_session(
        self, callback: Callable[[Session], T]
    ) -> Awaitable[T]:
        """Run *callback(session)* inside a thread-pool executor.

        The callback receives a :class:`Session`, must perform its work
        inside that session, and should **not** commit — this helper
        commits after the callback returns.
        """

        def _work() -> T:
            with self._db.session_factory() as session:
                result = callback(session)
                session.commit()
                return result

        return asyncio.to_thread(_work)

    def _with_session_ro(
        self, callback: Callable[[Session], T]
    ) -> Awaitable[T]:
        """Read-only variant of :meth:`_with_session` — no commit."""

        def _work() -> T:
            with self._db.session_factory() as session:
                return callback(session)

        return asyncio.to_thread(_work)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        message_id: int,
        reply_to_message_id: int | None = None,
        correlation_id: str | None = None,
    ) -> Operation:
        """Persist a new operation in ``RECEIVED`` state and return it.

        Parameters
        ----------
        chat_id:
            Telegram chat (conversation) ID.
        user_id:
            Telegram user who originated the request.
        text:
            The original free-form text of the user's request.
        message_id:
            Telegram message ID of the user's original request message.
        reply_to_message_id:
            Optional Telegram message ID this operation is replying to.
        correlation_id:
            Optional correlation / trace identifier for observability.

        Returns
        -------
        Operation
            The newly created operation (detached from session).
        """
        return await self._with_session(
            lambda session: _do_create(
                session,
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
            )
        )

    async def get(self, operation_id: str) -> Operation | None:
        """Fetch an operation by primary key, or *None* if not found."""
        return await self._with_session_ro(lambda session: session.get(Operation, operation_id))

    async def transition_status(
        self,
        operation_id: str,
        target: OperationState | str,
        *,
        expected_current: OperationState | str | None = None,
    ) -> bool:
        """Validate and atomically compare-and-swap an operation state.

        Terminal sources are absorbing.  A stale ``expected_current`` or a
        concurrent winner returns ``False`` without mutating the row.
        """
        return await self._with_session(
            lambda session: _do_transition_status(
                session,
                operation_id,
                target,
                expected_current=expected_current,
            )
        )

    async def update_status(
        self,
        operation_id: str,
        status: OperationState | str,
    ) -> bool:
        """Guarded compatibility wrapper around :meth:`transition_status`.

        Unlike the former unconditional update this method cannot resurrect a
        terminal operation or bypass :class:`StateMachine` validation.
        """
        return await self.transition_status(operation_id, status)

    async def update_progress(
        self,
        operation_id: str,
        *,
        current_stage: str,
        current_step: int,
        total_steps: int,
        tool_call_id: str | None = None,
    ) -> None:
        """Update the progress counters / stage label for an operation.

        Parameters
        ----------
        operation_id:
            Target operation UUID.
        current_stage:
            Human-readable label for the current pipeline stage.
        current_step:
            Zero-based index of the current sub-step.
        total_steps:
            Total sub-steps expected in the current stage.
        tool_call_id:
            Optional tool-invocation identifier to associate.
        """
        return await self._with_session(
            lambda session: _do_update_progress(
                session,
                operation_id,
                current_stage=current_stage,
                current_step=current_step,
                total_steps=total_steps,
                tool_call_id=tool_call_id,
            )
        )

    async def claim_progress_delivery(self, operation_id: str) -> bool:
        """Atomically reserve the one progress-message send for an operation."""
        return await self._with_session(
            lambda session: _do_claim_progress_delivery(session, operation_id)
        )

    async def save_progress_message_id(self, operation_id: str, message_id: int) -> bool:
        """Finalize a previously claimed progress-message delivery."""
        return await self._with_session(
            lambda session: _do_finalize_progress_message_id(
                session, operation_id, message_id
            )
        )

    async def claim_final_delivery(self, operation_id: str) -> FinalDeliveryClaim:
        """Acquire a durable at-most-once claim before Telegram ``sendMessage``."""
        return await self._with_session(
            lambda session: _do_claim_final_delivery(session, operation_id)
        )

    async def reserve_tool_execution(
        self,
        operation_id: str,
        tool_name: str,
        step_index: int,
        tool_call_id: str | None = None,
        call_hash: str | None = None,
    ) -> tuple[str, str]:
        """Reserve a tool execution slot (PENDING). Returns (claim_status, key).
        claim_status can be:
          'RESERVED' - newly reserved for execution
          'COMPLETED' - already executed and settled
          'PENDING' - previously reserved but crashed before settlement
          'UNCERTAIN' - ambiguous state or failed reservation
        """
        def _do_reserve(session: Session) -> tuple[str, str]:
            unique_suffix = tool_call_id or call_hash or "0"
            key = f"{tool_name}:{step_index}:{unique_suffix}"
            
            # Read current row state inside session transaction
            op = session.get(Operation, operation_id)
            if op is None:
                return ("UNCERTAIN", "")
            
            executed_tools = dict(op.executed_tools or {}) if isinstance(op.executed_tools, dict) else {}
            if key in executed_tools:
                entry = executed_tools[key]
                status = str(entry.get("status")) if isinstance(entry, dict) else "COMPLETED"
                return (status, key)
            
            # Atomic conditional update: update executed_tools ONLY if key is not yet present
            # We fetch current json string representation to ensure compare-and-swap semantics
            new_executed_tools = dict(executed_tools)
            new_executed_tools[key] = {"status": "PENDING"}
            
            # Perform atomic CAS update
            result = session.execute(
                update(Operation)
                .where(
                    Operation.id == operation_id,
                    Operation.executed_tools == executed_tools
                )
                .values(executed_tools=new_executed_tools)
            )
            assert isinstance(result, CursorResult)
            if result.rowcount == 1:
                return ("RESERVED", key)
            
            # If rowcount == 0, another worker won the race; re-fetch updated state
            session.expire(op)
            re_op = session.get(Operation, operation_id)
            if re_op:
                re_tools = dict(re_op.executed_tools or {}) if isinstance(re_op.executed_tools, dict) else {}
                if key in re_tools:
                    entry = re_tools[key]
                    status = str(entry.get("status")) if isinstance(entry, dict) else "COMPLETED"
                    return (status, key)
            return ("UNCERTAIN", key)
            
        return await self._with_session(_do_reserve)

    async def settle_tool_execution(
        self,
        operation_id: str,
        key: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Settle a reserved tool execution to COMPLETED with optional result payload."""
        def _do_settle(session: Session) -> None:
            op = session.get(Operation, operation_id)
            if op is None:
                return
            executed_tools = dict(op.executed_tools or {}) if isinstance(op.executed_tools, dict) else {}
            executed_tools[key] = {"status": "COMPLETED", "result": result or {}}
            op.executed_tools = executed_tools
            session.flush()
        return await self._with_session(_do_settle)

    async def claim_tool_execution(
        self,
        operation_id: str,
        tool_name: str,
        step_index: int,
        tool_call_id: str | None = None,
        call_hash: str | None = None,
    ) -> bool:
        """Backward-compatible claim for simple tool execution."""
        status, _ = await self.reserve_tool_execution(
            operation_id, tool_name, step_index, tool_call_id, call_hash
        )
        return status == "RESERVED"

    async def claim_progress_cleanup(
        self,
        operation_id: str,
        progress_message_id: int,
    ) -> bool:
        """Claim the one best-effort delete after terminal commit."""
        return await self._with_session(
            lambda session: _do_claim_progress_cleanup(
                session, operation_id, progress_message_id
            )
        )

    async def add_final_message_id(self, operation_id: str, message_id: int) -> None:
        """Append a message ID to the final-response manifest.

        The manifest is a JSON array of Telegram message IDs that together
        constitute the final response.  This method reads the existing list,
        appends, and writes it back atomically.
        """
        return await self._with_session(
            lambda session: _do_add_final_message_id(session, operation_id, message_id)
        )

    async def set_flow_id(self, operation_id: str, flow_id: str) -> None:
        """Bind the core-side flow to this operation.

        Chat-scoped cancellation (``/stop``) needs to find the flow that an
        active chat operation is waiting on without trusting the user to quote
        a flow id back at us.
        """
        return await self._with_session(
            lambda session: _do_update_column(session, operation_id, "flow_id", flow_id)
        )

    async def set_last_error(self, operation_id: str, error: str) -> None:
        """Store the error message on an operation."""
        return await self._with_session(
            lambda session: _do_update_column(session, operation_id, "last_error", error)
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def find_by_message_id(self, chat_id: int, message_id: int) -> Operation | None:
        """Find an operation by its origin message ID, or *None*.

        Looks up the row where ``chat_id`` and ``origin_message_id`` match
        exactly.  Returns the most recent match if there are duplicates
        (sorted by ``created_at`` descending).
        """
        return await self._with_session_ro(
            lambda session: (
                session.execute(
                    select(Operation)
                    .where(
                        Operation.chat_id == chat_id,
                        Operation.origin_message_id == message_id,
                    )
                    .order_by(Operation.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        )

    async def find_active_by_chat(self, chat_id: int) -> Operation | None:
        """Return the most recent non-terminal operation for a chat.

        An "active" operation is one whose status is **not** a terminal
        state (``SUCCEEDED``, ``FAILED``, ``CANCELLED``).  If none exists,
        returns *None*.

        The result is ordered by ``created_at`` descending so the caller
        gets the latest active operation per chat.
        """
        return await self._with_session_ro(
            lambda session: (
                session.execute(
                    select(Operation)
                    .where(
                        Operation.chat_id == chat_id,
                        ~Operation.status.in_(_TERMINAL_STATUS_VALUES),
                    )
                    .order_by(Operation.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        )

    async def find_active_by_progress_message(
        self,
        chat_id: int,
        progress_message_id: int,
    ) -> Operation | None:
        """Find the exact non-terminal operation owning a progress message."""
        return await self._with_session_ro(
            lambda session: (
                session.execute(
                    select(Operation)
                    .where(
                        Operation.chat_id == chat_id,
                        Operation.progress_message_id == progress_message_id,
                        ~Operation.status.in_(_TERMINAL_STATUS_VALUES),
                    )
                    .order_by(Operation.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        )


# ═══════════════════════════════════════════════════════════════════════
# Module-level helpers  (run inside the thread-pool, receive a Session)
# ═══════════════════════════════════════════════════════════════════════


def _do_create(
    session: Session,
    *,
    chat_id: int,
    user_id: int,
    text: str,
    message_id: int,
    reply_to_message_id: int | None,
) -> Operation:
    """Build, persist, and return a new ``RECEIVED`` operation."""
    op = Operation(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        origin_message_id=message_id,
        reply_to_message_id=reply_to_message_id,
    )
    session.add(op)
    session.flush()
    # Expunge so the caller can use the object outside this session
    session.expunge(op)
    return op


def _normalise_state(value: OperationState | str) -> OperationState:
    if isinstance(value, OperationState):
        return value
    return OperationState(str(value).upper())


def _do_transition_status(
    session: Session,
    operation_id: str,
    target: OperationState | str,
    *,
    expected_current: OperationState | str | None,
) -> bool:
    """Validate an edge and CAS it against the persisted source state."""
    op = session.get(Operation, operation_id)
    if op is None:
        raise OperationNotFound(operation_id)

    current = _normalise_state(op.status)
    target_state = _normalise_state(target)
    if expected_current is not None and current is not _normalise_state(expected_current):
        return False
    if StateMachine.is_terminal(current):
        return False
    if current is target_state:
        return True
    if not StateMachine.can_transition(current, target_state):
        return False

    result = session.execute(
        update(Operation)
        .where(Operation.id == operation_id, Operation.status == current.value)
        .values(status=target_state.value)
    )
    assert isinstance(result, CursorResult)
    return result.rowcount == 1


def _do_claim_progress_delivery(session: Session, operation_id: str) -> bool:
    """CAS ``NULL`` to a sentinel before the one progress send."""
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.progress_message_id.is_(None),
            ~Operation.status.in_(_TERMINAL_STATUS_VALUES),
            or_(
                Operation.current_stage.is_(None),
                ~Operation.current_stage.startswith(_FINAL_DELIVERY_CLAIM_PREFIX),
            ),
        )
        .values(progress_message_id=_PROGRESS_DELIVERY_CLAIM)
    )
    assert isinstance(result, CursorResult)
    if result.rowcount == 1:
        return True
    if session.get(Operation, operation_id) is None:
        raise OperationNotFound(operation_id)
    return False


def _do_finalize_progress_message_id(
    session: Session,
    operation_id: str,
    message_id: int,
) -> bool:
    """Replace only this process's durable progress-delivery sentinel."""
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.progress_message_id == _PROGRESS_DELIVERY_CLAIM,
        )
        .values(progress_message_id=message_id)
    )
    assert isinstance(result, CursorResult)
    if result.rowcount == 1:
        return True
    if session.get(Operation, operation_id) is None:
        raise OperationNotFound(operation_id)
    return False


def _do_claim_final_delivery(
    session: Session,
    operation_id: str,
) -> FinalDeliveryClaim:
    """Durably claim final delivery using the existing ``current_stage`` field."""
    op = session.get(Operation, operation_id)
    if op is None:
        raise OperationNotFound(operation_id)

    final_ids = list(op.final_message_ids or [])
    if final_ids:
        return FinalDeliveryClaim(
            claimed=False,
            existing_message_id=int(final_ids[0]),
        )

    current = _normalise_state(op.status)
    stage = op.current_stage
    if StateMachine.is_terminal(current):
        return FinalDeliveryClaim(claimed=False, in_flight=True)
    if op.progress_message_id == _PROGRESS_DELIVERY_CLAIM:
        return FinalDeliveryClaim(claimed=False, in_flight=True)
    if stage and (
        stage.startswith(_FINAL_DELIVERY_CLAIM_PREFIX)
        or stage == _PROGRESS_CLEANUP_CLAIM
    ):
        return FinalDeliveryClaim(claimed=False, in_flight=True)

    token = f"{_FINAL_DELIVERY_CLAIM_PREFIX}{uuid.uuid4()}"
    stage_guard = (
        Operation.current_stage.is_(None)
        if stage is None
        else Operation.current_stage == stage
    )
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == current.value,
            stage_guard,
        )
        .values(current_stage=token)
    )
    assert isinstance(result, CursorResult)
    if result.rowcount != 1:
        return FinalDeliveryClaim(claimed=False, in_flight=True)
    return FinalDeliveryClaim(claimed=True, token=token)


def _do_claim_progress_cleanup(
    session: Session,
    operation_id: str,
    progress_message_id: int,
) -> bool:
    """CAS the cleanup marker so duplicate finals cannot delete twice."""
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.progress_message_id == progress_message_id,
            Operation.status.in_(_TERMINAL_STATUS_VALUES),
            or_(
                Operation.current_stage.is_(None),
                Operation.current_stage != _PROGRESS_CLEANUP_CLAIM,
            ),
        )
        .values(current_stage=_PROGRESS_CLEANUP_CLAIM)
    )
    assert isinstance(result, CursorResult)
    if result.rowcount == 1:
        return True
    if session.get(Operation, operation_id) is None:
        raise OperationNotFound(operation_id)
    return False


def _do_update_progress(
    session: Session,
    operation_id: str,
    *,
    current_stage: str,
    current_step: int,
    total_steps: int,
    tool_call_id: str | None,
) -> None:
    """Update progress fields.  Builds the value dict dynamically."""
    values: dict[str, Any] = {
        "current_stage": current_stage,
        "current_step": current_step,
        "total_steps": total_steps,
    }
    if tool_call_id is not None:
        values["tool_call_id"] = tool_call_id

    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            ~Operation.status.in_(_TERMINAL_STATUS_VALUES),
            or_(
                Operation.current_stage.is_(None),
                ~Operation.current_stage.startswith(_FINAL_DELIVERY_CLAIM_PREFIX),
            ),
        )
        .values(**values)
    )
    assert isinstance(result, CursorResult)
    if result.rowcount == 0:
        if session.get(Operation, operation_id) is None:
            raise OperationNotFound(operation_id)


def _do_update_column(
    session: Session,
    operation_id: str,
    column: str,
    value: Any,
) -> None:
    """Set a single column to *value* on the given operation."""
    result = session.execute(
        update(Operation).where(Operation.id == operation_id).values(**{column: value})
    )
    assert isinstance(result, CursorResult)
    if result.rowcount == 0:
        raise OperationNotFound(operation_id)


def _do_add_final_message_id(
    session: Session,
    operation_id: str,
    message_id: int,
) -> None:
    """Read the JSON array, append *message_id*, and write it back."""
    if isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("final message receipt must be a positive integer")
    op = session.get(Operation, operation_id)
    if op is None:
        raise OperationNotFound(operation_id)

    current = list(op.final_message_ids or [])
    if message_id not in current:
        current.append(message_id)
        op.final_message_ids = current
        session.flush()


# ── Module-level re-exports ───────────────────────────────────────────

__all__ = [
    "FinalDeliveryClaim",
    "OperationNotFound",
    "OperationStore",
]
