"""Autonomous Goal Orchestration (M2) — state machines.

Pure enums + transition rules for Goal, Flow, Wake and Service states.
No side effects — the store enforces these.
"""
from __future__ import annotations

from enum import StrEnum


class GoalState(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


GOAL_TERMINAL = frozenset(
    {GoalState.SUCCEEDED, GoalState.FAILED, GoalState.CANCELLED}
)

_GOAL_TRANSITIONS: dict[GoalState, set[GoalState]] = {
    GoalState.PENDING: {GoalState.ACTIVE, GoalState.CANCELLED},
    GoalState.ACTIVE: {
        GoalState.WAITING, GoalState.BLOCKED, GoalState.SUCCEEDED,
        GoalState.FAILED, GoalState.CANCELLED,
    },
    GoalState.WAITING: {
        GoalState.ACTIVE,
        GoalState.CANCELLED,
        # Terminal while parked: work may finish/resolve while the goal sits on
        # a durable wake (Grok audit Crash/Recovery MAJOR).
        GoalState.SUCCEEDED,
        GoalState.FAILED,
    },
    GoalState.BLOCKED: {
        GoalState.ACTIVE,
        GoalState.CANCELLED,
        GoalState.SUCCEEDED,
        GoalState.FAILED,
    },
    GoalState.SUCCEEDED: set(),
    GoalState.FAILED: set(),
    GoalState.CANCELLED: set(),
}


def require_goal_transition(cur: GoalState, nxt: GoalState) -> None:
    if nxt not in _GOAL_TRANSITIONS.get(cur, set()):
        raise GoalStateError(f"invalid goal transition: {cur.value} -> {nxt.value}")


class GoalStateError(ValueError):
    pass


class FlowState(StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"  # replaced by a newer flow revision (REPLAN keeps history)


_flow_terminal = frozenset(
    {FlowState.SUCCEEDED, FlowState.FAILED, FlowState.CANCELLED, FlowState.SUPERSEDED}
)


class WakeKind(StrEnum):
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TIMER_EXPIRED = "TimerExpired"
    APPROVAL_GRANTED = "ApprovalGranted"
    OWNER_MESSAGE = "OwnerMessage"
    EXTERNAL_PROCESS_COMPLETED = "ExternalProcessCompleted"
    SERVICE_AVAILABLE = "ServiceAvailable"
    MANUAL_RESUME = "ManualResume"


class WakeStatus(StrEnum):
    PENDING = "PENDING"
    FIRED = "FIRED"
    IGNORED = "IGNORED"


class ServiceState(StrEnum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_LOW = "QUOTA_LOW"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class Capacity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class FailureClass(StrEnum):
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILURE = "AUTH_FAILURE"
    TIMEOUT = "TIMEOUT"
    PROCESS_CRASH = "PROCESS_CRASH"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    TASK_FAILURE = "TASK_FAILURE"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    UNKNOWN = "UNKNOWN"


class JudgeDecision(StrEnum):
    DONE = "DONE"
    CONTINUE = "CONTINUE"
    WAIT = "WAIT"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"
