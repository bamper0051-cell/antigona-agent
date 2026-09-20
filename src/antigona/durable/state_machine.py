"""Formal task/step state machine.

The transition graph here is the single authority: every state mutation in the
codebase must pass through :func:`check_task_transition` /
:func:`check_step_transition` before it touches a row, and every *refused*
transition is journalled with :func:`record_rejection`. ``DONE`` is deliberately
absent from the graph exposed to the generic repository API — only the Verifier
service holds the capability to perform ``VERIFYING -> DONE`` (documented as
:data:`VERIFIER_ONLY_TRANSITIONS`), enforced out of band by a bearer-guarded
compare-and-set. This keeps AGENTS.md's "only Verifier sets DONE" invariant true
by construction rather than by convention.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models import StateTransition, StepState, TaskState


class InvalidTransition(ValueError):
    """Raised when a caller requests a transition the graph forbids."""


class ConcurrentUpdate(RuntimeError):
    """Raised when an optimistic-lock (revision CAS) update matched no row."""


#: Absorbing states. Once a flow reaches one of these it never transitions again;
#: sticky cancel relies on ``CANCELLED`` living here.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.DONE,
        TaskState.FAILED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.TIMEOUT,
        TaskState.POLICY_DENIED,
    }
)

#: Execution-claiming states. Entering ANY of these is an execution claim and
#: therefore requires trusted (VERIFIED) evidence BEFORE the materialized state
#: changes and BEFORE the event is appended. This is the SINGLE source of truth
#: used by TaskRegistry (transition guard), StateMachine and Consistency.
EXECUTION_CLAIMING_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.TOOL_EXECUTING,
        TaskState.OBSERVING,
        TaskState.VERIFYING,
        TaskState.DONE,
    }
)

#: Legal task transitions reachable through the generic (repository) API.
#: ``CANCELLED`` is reachable from every non-terminal state so cancellation is
#: always honoured; ``DONE`` is intentionally excluded (see module docstring).
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.RECEIVED: frozenset({TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.QUEUED: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.PLANNING: frozenset(
        {
            TaskState.READY,
            TaskState.WAITING_APPROVAL,
            TaskState.TOOL_EXECUTING,
            TaskState.CANCELLED,
            TaskState.POLICY_DENIED,
        }
    ),
    TaskState.READY: frozenset({TaskState.TOOL_EXECUTING, TaskState.CANCELLED}),
    TaskState.WAITING_APPROVAL: frozenset(
        {TaskState.TOOL_EXECUTING, TaskState.POLICY_DENIED, TaskState.CANCELLED}
    ),
    TaskState.TOOL_EXECUTING: frozenset(
        {
            TaskState.OBSERVING,
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.TIMEOUT,
            TaskState.CANCELLED,
        }
    ),
    TaskState.OBSERVING: frozenset({TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.RETRY_SCHEDULED,
            TaskState.REPLAN_REQUESTED,
            TaskState.WAITING_USER,
            TaskState.WAITING_APPROVAL,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        }
    ),
    # ── Retry / replan ────────────────────────────────────────────────
    TaskState.RETRY_SCHEDULED: frozenset(
        {TaskState.TOOL_EXECUTING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.REPLAN_REQUESTED: frozenset(
        {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    # ── User-interaction branches ─────────────────────────────────────
    TaskState.WAITING_USER: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    # ── Pause / resume ────────────────────────────────────────────────
    TaskState.PAUSED: frozenset({TaskState.TOOL_EXECUTING, TaskState.CANCELLED}),
}

#: Transitions only the Verifier capability may perform, never the generic API.
VERIFIER_ONLY_TRANSITIONS: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {(TaskState.VERIFYING, TaskState.DONE)}
)

#: Journal ``entity_type`` for an OBSERVATION — knowledge about a flow that is
#: real and must stay readable, but that changes NO state (FP-L02).
#:
#: The append-only ``state_transitions`` table is the only journal we have, so an
#: observation is stored as a row typed ``observation`` instead of a fabricated
#: ``X -> X`` "transition": ``from_state == to_state`` on such a row is a marker
#: of "nothing transitioned", never an edge. Readers that walk real transitions
#: (metrics, audit, anomaly checks) must filter by this type or match on
#: ``entity_type in {"task", "step"}`` — a self-repeating row of any other type
#: is a defect, not history.
OBSERVATION_ENTITY_TYPE: str = "observation"

#: Legal step transitions.
STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING, StepState.CANCELLED}),
    StepState.RUNNING: frozenset(
        {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED}
    ),
}


def check_task_transition(
    current: TaskState, target: TaskState, *, cancellation_requested: bool
) -> None:
    """Raise :class:`InvalidTransition` unless ``current -> target`` is legal.

    Terminal states are absorbing, unknown edges are forbidden, and a task whose
    cancellation has been requested may only move to ``CANCELLED`` (sticky cancel).
    """
    if current in TERMINAL_STATES or target not in TASK_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"{current.value} -> {target.value} is forbidden")
    if cancellation_requested and target is not TaskState.CANCELLED:
        raise InvalidTransition("cancellation is sticky")


def check_step_transition(current: StepState, target: StepState) -> None:
    """Raise :class:`InvalidTransition` unless ``current -> target`` is legal for a step."""
    if target not in STEP_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"step {current.value} -> {target.value} is forbidden")


def transition(
    current: TaskState,
    target: TaskState,
    *,
    cancellation_requested: bool,
    verifier_capability: bool = False,
) -> None:
    """Single authority for task-state transitions.

    ``check_task_transition`` remains the authoritative graph checker for every
    generic edge. This wrapper adds capability guards for Verifier-only DONE and
    keeps terminal/cancel invariants fail-closed.
    """
    if current in TERMINAL_STATES:
        raise InvalidTransition(f"{current.value} -> {target.value} is forbidden")
    if cancellation_requested and target is not TaskState.CANCELLED:
        raise InvalidTransition("cancellation is sticky")
    if target is TaskState.DONE:
        if current is not TaskState.VERIFYING:
            raise InvalidTransition(f"{current.value} -> {target.value} is forbidden")
        if not verifier_capability:
            raise InvalidTransition("VERIFYING -> DONE requires verifier capability")
        return
    check_task_transition(current, target, cancellation_requested=cancellation_requested)


def guard_verifying_done(evidence_verified: bool) -> None:
    """Require verified evidence before accepting ``VERIFYING -> DONE``."""
    if not evidence_verified:
        raise InvalidTransition("VERIFYING -> DONE requires verified evidence")


def record_rejection(
    session: Session,
    *,
    task_id: str,
    entity_id: str,
    entity_type: str,
    from_state: str,
    to_state: str,
    reason: str,
    actor: str,
    correlation_id: str | None = None,
) -> None:
    """Append an immutable ``REJECTED`` row to ``state_transitions``.

    The row is added to the caller's session and flushed (never committed here),
    so a refusal becomes part of the caller's unit of work rather than opening a
    second connection that could deadlock against an in-flight write. Callers that
    proceed to commit persist the audit; callers that abort discard it together
    with the rejected change — in both cases the accepted-transition history stays
    consistent with the refusal record.
    """
    session.add(
        StateTransition(
            task_id=task_id,
            entity_id=entity_id,
            entity_type=entity_type,
            from_state=from_state,
            to_state=to_state,
            reason=f"REJECTED {from_state}->{to_state}: {reason}",
            actor=actor,
            correlation_id=correlation_id or str(uuid.uuid4()),
        )
    )
    session.flush()
