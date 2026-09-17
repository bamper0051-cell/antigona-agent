from __future__ import annotations

import pytest

from antigona.durable.state_machine import InvalidTransition, guard_verifying_done, transition
from antigona.models import TaskState


def test_terminal_to_active_is_forbidden() -> None:
    with pytest.raises(InvalidTransition):
        transition(
            TaskState.DONE,
            TaskState.PLANNING,
            cancellation_requested=False,
            verifier_capability=True,
        )


def test_skip_state_edge_is_forbidden() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskState.RECEIVED, TaskState.VERIFYING, cancellation_requested=False)


def test_done_recompletion_is_forbidden() -> None:
    with pytest.raises(InvalidTransition):
        transition(
            TaskState.DONE,
            TaskState.DONE,
            cancellation_requested=False,
            verifier_capability=True,
        )


def test_verifying_done_requires_verified_evidence() -> None:
    with pytest.raises(InvalidTransition):
        guard_verifying_done(False)


def test_verifying_done_requires_verifier_capability() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskState.VERIFYING, TaskState.DONE, cancellation_requested=False)


def test_sticky_cancel_blocks_non_cancel_transition() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskState.PLANNING, TaskState.READY, cancellation_requested=True)
