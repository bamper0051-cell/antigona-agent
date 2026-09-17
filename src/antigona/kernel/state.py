"""Durable Execution Kernel (M1) — Task & Run state machines.

Pure, side-effect-free transition graphs for the kernel's two entities:

* :class:`TaskState` — logical task lifecycle
  ``PENDING → BLOCKED/READY → RUNNING → SUCCEEDED|FAILED|CANCELLED``
  (READY is re-entered for bounded retries; BLOCKED while dependencies unmet).

* :class:`RunState` — one execution attempt lifecycle
  ``READY → RUNNING → SUCCEEDED|FAILED|LOST|CANCELLED``
  (RETRY_WAIT between a retryable FAILED run and the next READY run).

Every transition is validated by an explicit graph; illegal transitions and
terminal-state mutations are rejected. This is the single authority for what
may happen to a task/run — the store enforces it before any write.
"""
from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


_TERMINAL_TASK: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)
_TERMINAL_RUN: frozenset[RunState] = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.LOST, RunState.CANCELLED}
)

_TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.BLOCKED, TaskState.READY, TaskState.CANCELLED}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.READY}
    ),
}

_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.READY: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.LOST,
            RunState.RETRY_WAIT,
        }
    ),
    RunState.RETRY_WAIT: frozenset({RunState.READY}),
}


def task_is_terminal(state: TaskState | str) -> bool:
    return state in _TERMINAL_TASK


def run_is_terminal(state: RunState | str) -> bool:
    return state in _TERMINAL_RUN


def task_can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    if from_state in _TERMINAL_TASK:
        return False
    return to_state in _TASK_TRANSITIONS.get(from_state, frozenset())


def run_can_transition(from_state: RunState, to_state: RunState) -> bool:
    if from_state in _TERMINAL_RUN:
        return False
    return to_state in _RUN_TRANSITIONS.get(from_state, frozenset())


class KernelStateError(Exception):
    """Raised on an illegal state transition (fail-fast at the app layer)."""


def require_task_transition(from_state: TaskState, to_state: TaskState) -> None:
    if not task_can_transition(from_state, to_state):
        raise KernelStateError(
            f"illegal task transition {from_state.value} -> {to_state.value}"
        )


def require_run_transition(from_state: RunState, to_state: RunState) -> None:
    if not run_can_transition(from_state, to_state):
        raise KernelStateError(
            f"illegal run transition {from_state.value} -> {to_state.value}"
        )
