"""Tests for the authoritative Gateway TaskState state machine (merged).

This validates that the single authoritative ``TaskState`` and its transition
graph (``TASK_TRANSITIONS``) correctly enforce the lifecycle rules previously
split across two parallel state machines.

States
------
    RECEIVED, CREATED, QUEUED, PLANNING, READY, WAITING_APPROVAL,
    TOOL_EXECUTING, OBSERVING, VERIFYING, RETRY_SCHEDULED, REPLAN_REQUESTED,
    WAITING_USER, PAUSED, BLOCKED, FAILED, CANCELLED, DONE, TIMEOUT,
    POLICY_DENIED, RUNNING

Terminal (absorbing) states
    ``DONE``, ``FAILED``, ``BLOCKED``, ``CANCELLED``, ``TIMEOUT``,
    ``POLICY_DENIED``

Key rules
    - ``DONE`` is **only** reachable from ``VERIFYING`` — the Verifier gate.
    - ``CANCELLED`` is reachable from every non-terminal state (sticky cancel).
    - ``QUEUED -> DONE``, ``EXECUTING -> DONE``, and any direct-to-``DONE``
      shortcut is **forbidden** by construction.
"""

from __future__ import annotations

import pytest

from antigona.durable.state_machine import (
    TASK_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    check_task_transition,
)
from antigona.models import TaskState

# ── Enum completeness ─────────────────────────────────────────────────────────


class TestEnum:
    """Section: TaskState has all states from both original machines."""

    def test_all_expected_states_present(self) -> None:
        expected = {
            "RECEIVED",
            "CREATED",
            "QUEUED",
            "PLANNING",
            "READY",
            "WAITING_APPROVAL",
            "TOOL_EXECUTING",
            "RUNNING",
            "OBSERVING",
            "VERIFYING",
            "RETRY_SCHEDULED",
            "REPLAN_REQUESTED",
            "WAITING_USER",
            "PAUSED",
            "BLOCKED",
            "FAILED",
            "CANCELLED",
            "DONE",
            "TIMEOUT",
            "POLICY_DENIED",
        }
        actual = {s.value for s in TaskState}
        assert actual == expected


# ── Terminal state detection ──────────────────────────────────────────────────


class TestIsTerminal:
    """Section: is_terminal() correctly identifies absorbing states."""

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
            TaskState.TIMEOUT,
            TaskState.POLICY_DENIED,
        ],
    )
    def test_terminal_states(self, state: TaskState) -> None:
        assert state in TERMINAL_STATES

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.CREATED,
            TaskState.RECEIVED,
            TaskState.QUEUED,
            TaskState.PLANNING,
            TaskState.READY,
            TaskState.WAITING_APPROVAL,
            TaskState.TOOL_EXECUTING,
            TaskState.RUNNING,
            TaskState.OBSERVING,
            TaskState.VERIFYING,
            TaskState.RETRY_SCHEDULED,
            TaskState.REPLAN_REQUESTED,
            TaskState.WAITING_USER,
            TaskState.PAUSED,
        ],
    )
    def test_non_terminal_states(self, state: TaskState) -> None:
        assert state not in TERMINAL_STATES


# ── Legal transitions (spec) ──────────────────────────────────────────────────


class TestLegalTransitions:
    """Section: each explicitly listed edge is accepted."""

    # Main pipeline (Gateway path)
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (TaskState.RECEIVED, TaskState.QUEUED),
            (TaskState.QUEUED, TaskState.PLANNING),
            (TaskState.PLANNING, TaskState.WAITING_APPROVAL),
            (TaskState.PLANNING, TaskState.TOOL_EXECUTING),
            (TaskState.WAITING_APPROVAL, TaskState.TOOL_EXECUTING),
            (TaskState.TOOL_EXECUTING, TaskState.OBSERVING),
            (TaskState.OBSERVING, TaskState.VERIFYING),
        ],
    )
    def test_gateway_main_pipeline(
        self, from_state: TaskState, to_state: TaskState
    ) -> None:
        check_task_transition(from_state, to_state, cancellation_requested=False)

    # Main pipeline (Agent-loop path — merged states)
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (TaskState.CREATED, TaskState.QUEUED),
            (TaskState.PLANNING, TaskState.READY),
            (TaskState.READY, TaskState.TOOL_EXECUTING),
            (TaskState.TOOL_EXECUTING, TaskState.OBSERVING),
            (TaskState.OBSERVING, TaskState.VERIFYING),
        ],
    )
    def test_agent_loop_main_pipeline(
        self, from_state: TaskState, to_state: TaskState
    ) -> None:
        check_task_transition(from_state, to_state, cancellation_requested=False)

    # VERIFYING fan-out (merged Gateway + taskflow transitions)
    @pytest.mark.parametrize(
        "to_state",
        [
            TaskState.RETRY_SCHEDULED,
            TaskState.REPLAN_REQUESTED,
            TaskState.WAITING_USER,
            TaskState.WAITING_APPROVAL,
            TaskState.BLOCKED,
            TaskState.FAILED,
        ],
    )
    def test_verifying_fan_out(self, to_state: TaskState) -> None:
        check_task_transition(TaskState.VERIFYING, to_state, cancellation_requested=False)

    # Retry / replan loops
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (TaskState.RETRY_SCHEDULED, TaskState.TOOL_EXECUTING),
            (TaskState.REPLAN_REQUESTED, TaskState.PLANNING),
        ],
    )
    def test_retry_and_replan(self, from_state: TaskState, to_state: TaskState) -> None:
        check_task_transition(from_state, to_state, cancellation_requested=False)

    # User-interaction return
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (TaskState.WAITING_USER, TaskState.PLANNING),
            (TaskState.WAITING_APPROVAL, TaskState.TOOL_EXECUTING),
        ],
    )
    def test_user_interaction(
        self, from_state: TaskState, to_state: TaskState
    ) -> None:
        check_task_transition(from_state, to_state, cancellation_requested=False)

    # Pause / resume
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (TaskState.TOOL_EXECUTING, TaskState.PAUSED),
            (TaskState.PAUSED, TaskState.TOOL_EXECUTING),
        ],
    )
    def test_pause_resume(self, from_state: TaskState, to_state: TaskState) -> None:
        check_task_transition(from_state, to_state, cancellation_requested=False)


# ── CANCELLED from every non-terminal state ───────────────────────────────────


def _non_terminal_states() -> set[TaskState]:
    """Return non-terminal states that have at least one transition defined."""
    return {s for s in TaskState if s not in TERMINAL_STATES and s in TASK_TRANSITIONS}


class TestCancelledFromNonTerminal:
    """Section: CANCELLED is reachable from every non-terminal state."""

    @pytest.mark.parametrize("from_state", list(_non_terminal_states()))
    def test_cancelled_reachable(self, from_state: TaskState) -> None:
        check_task_transition(from_state, TaskState.CANCELLED, cancellation_requested=False)


# ── Forbidden transitions (spec blocked paths) ────────────────────────────────


class TestForbiddenTransitions:
    """Section: each explicitly *forbidden* edge is rejected."""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            # DONE is only reachable from VERIFYING (verifier-only)
            (TaskState.QUEUED, TaskState.DONE),
            (TaskState.TOOL_EXECUTING, TaskState.DONE),
            (TaskState.OBSERVING, TaskState.DONE),
            (TaskState.FAILED, TaskState.DONE),
            (TaskState.CANCELLED, TaskState.DONE),
            # Non-terminal → non-terminal that is not in the graph
            (TaskState.CREATED, TaskState.DONE),
            (TaskState.CREATED, TaskState.TOOL_EXECUTING),
            (TaskState.CREATED, TaskState.VERIFYING),
            (TaskState.QUEUED, TaskState.READY),
            (TaskState.QUEUED, TaskState.TOOL_EXECUTING),
            # PLANNING -> TOOL_EXECUTING is legal in merged machine (intentionally omitted)
            (TaskState.READY, TaskState.OBSERVING),
            (TaskState.TOOL_EXECUTING, TaskState.VERIFYING),
            (TaskState.OBSERVING, TaskState.DONE),
            (TaskState.OBSERVING, TaskState.BLOCKED),
            (TaskState.VERIFYING, TaskState.QUEUED),
            (TaskState.VERIFYING, TaskState.READY),
            (TaskState.VERIFYING, TaskState.TOOL_EXECUTING),
            (TaskState.RETRY_SCHEDULED, TaskState.PLANNING),
            (TaskState.REPLAN_REQUESTED, TaskState.TOOL_EXECUTING),
            (TaskState.WAITING_USER, TaskState.TOOL_EXECUTING),
            (TaskState.WAITING_APPROVAL, TaskState.PLANNING),
            (TaskState.PAUSED, TaskState.PLANNING),
        ],
    )
    def test_illegal_non_terminal_targets(
        self, from_state: TaskState, to_state: TaskState
    ) -> None:
        with pytest.raises(InvalidTransition):
            check_task_transition(from_state, to_state, cancellation_requested=False)


# ── Terminal states reject all transitions ────────────────────────────────────


class TestTerminalRejectsAll:
    """Section: terminal states may not transition to anything."""

    @pytest.mark.parametrize("from_state", list(TERMINAL_STATES))
    def test_no_transition_from_terminal(self, from_state: TaskState) -> None:
        for to_state in TaskState:
            if from_state == to_state:
                # Self-transition triggers "is terminal" error
                with pytest.raises(InvalidTransition):
                    check_task_transition(from_state, to_state, cancellation_requested=False)
            else:
                with pytest.raises(InvalidTransition):
                    check_task_transition(from_state, to_state, cancellation_requested=False)


# ── check_task_transition raises InvalidTransition ────────────────────────────


class TestInvalidTransitionRaised:
    """Section: check_task_transition() raises on illegal edges."""

    def test_legal_pass(self) -> None:
        # Should not raise
        check_task_transition(TaskState.RECEIVED, TaskState.QUEUED, cancellation_requested=False)

    def test_terminal_source_raises(self) -> None:
        with pytest.raises(InvalidTransition, match="forbidden"):
            check_task_transition(TaskState.DONE, TaskState.FAILED, cancellation_requested=False)

    def test_forbidden_edge_raises(self) -> None:
        with pytest.raises(InvalidTransition, match="forbidden"):
            check_task_transition(
                TaskState.QUEUED, TaskState.DONE, cancellation_requested=False
            )

    def test_cancellation_requested_raises_when_not_cancelled(self) -> None:
        with pytest.raises(InvalidTransition, match="sticky"):
            check_task_transition(
                TaskState.PLANNING,
                TaskState.READY,
                cancellation_requested=True,
            )

    def test_cancellation_requested_allows_cancelled(self) -> None:
        # Should not raise (sticky cancel allows CANCELLED)
        check_task_transition(
            TaskState.PLANNING,
            TaskState.CANCELLED,
            cancellation_requested=True,
        )

    def test_cancellation_requested_on_terminal_raises(self) -> None:
        with pytest.raises(InvalidTransition, match="forbidden"):
            check_task_transition(
                TaskState.DONE,
                TaskState.CANCELLED,
                cancellation_requested=True,
            )
