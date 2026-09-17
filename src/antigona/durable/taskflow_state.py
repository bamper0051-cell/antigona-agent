"""
DEPRECATED — use ``antigona.models.TaskState`` / ``antigona.durable.state_machine`` instead.

This module previously held ``TaskFlowState`` (16-state enum) and
``TaskFlowStateMachine`` — a parallel state machine that duplicated the
authoritative Gateway ``TaskState`` from ``antigona.models``.

All states have been merged into ``antigona.models.TaskState`` and all
transitions into ``antigona.durable.state_machine.TASK_TRANSITIONS``.
The agent loop now uses the Gateway state machine directly.

These re-exports exist for backward compatibility only.
"""

from __future__ import annotations

from antigona.durable.state_machine import (
    TASK_TRANSITIONS as _TASK_TRANSITIONS,
)
from antigona.durable.state_machine import (
    InvalidTransition,
)
from antigona.models import TaskState as TaskFlowState

# ── Backward-compat helpers ────────────────────────────────────────────────

_TERMINAL_TASKFLOW_STATES = frozenset(
    {
        TaskFlowState.DONE,
        TaskFlowState.FAILED,
        TaskFlowState.BLOCKED,
        TaskFlowState.CANCELLED,
    }
)

_TASKFLOW_TRANSITIONS: dict[TaskFlowState, frozenset[TaskFlowState]] = {
    k: frozenset(v) for k, v in _TASK_TRANSITIONS.items()
}


class TaskFlowStateMachine:
    """DEPRECATED — use :func:`antigona.durable.state_machine.check_task_transition` instead.

    Kept for backward compatibility.  Delegates to the authoritative Gateway
    state machine under the hood.
    """

    @staticmethod
    def is_terminal(state: TaskFlowState) -> bool:
        return state in _TERMINAL_TASKFLOW_STATES

    @staticmethod
    def can_transition(from_state: TaskFlowState, to_state: TaskFlowState) -> bool:
        if from_state in _TERMINAL_TASKFLOW_STATES:
            return False
        targets = _TASKFLOW_TRANSITIONS.get(from_state)
        if targets is None:
            return False
        return to_state in targets

    @staticmethod
    def guard_transition(
        from_state: TaskFlowState,
        to_state: TaskFlowState,
        *,
        cancellation_requested: bool = False,
    ) -> None:
        if from_state in _TERMINAL_TASKFLOW_STATES:
            raise InvalidTransition(f"{from_state.value} is terminal — no transitions allowed")
        if cancellation_requested and to_state is not TaskFlowState.CANCELLED:
            raise InvalidTransition(
                f"cancellation is sticky: {from_state.value} may only go to "
                f"CANCELLED, not {to_state.value}"
            )
        targets = _TASKFLOW_TRANSITIONS.get(from_state)
        if targets is None or to_state not in targets:
            raise InvalidTransition(
                f"{from_state.value} -> {to_state.value} is forbidden by "
                f"the task-flow transition graph"
            )

    @staticmethod
    def transition(
        from_state: TaskFlowState,
        to_state: TaskFlowState,
        *,
        cancellation_requested: bool = False,
    ) -> bool:
        try:
            TaskFlowStateMachine.guard_transition(
                from_state,
                to_state,
                cancellation_requested=cancellation_requested,
            )
            return True
        except InvalidTransition:
            return False


__all__ = [
    "InvalidTransition",
    "TaskFlowState",
    "TaskFlowStateMachine",
]
