from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from antigona.core.event_log import EventLog, EventRecord
from antigona.core.evidence_registry import EvidenceRegistry
from antigona.core.task_registry import TaskRegistry
from antigona.durable.state_machine import (
    EXECUTION_CLAIMING_STATES,
    TERMINAL_STATES,
    InvalidTransition,
)
from antigona.models import Approval, TaskState


@dataclass(frozen=True)
class ConsistencyReport:
    task_id: str
    consistent: bool
    checks: tuple[tuple[str, bool, str], ...]


def verify_task_consistency(
    task_id: str,
    *,
    task_registry: TaskRegistry,
    event_log: EventLog,
    evidence_registry: EvidenceRegistry,
) -> ConsistencyReport:
    checks: list[tuple[str, bool, str]] = []

    task = task_registry.get(task_id)
    events = event_log.replay(task_id)

    replay_ok = _check_replay_matches_registry(task_id=task_id, expected=task.status, event_log=event_log)
    checks.append(replay_ok)

    evidence_ok = _check_execution_events_have_trusted_evidence(
        task_id=task_id,
        events=events,
        evidence_registry=evidence_registry,
    )
    checks.append(evidence_ok)

    terminal_tail_ok = _check_no_events_after_terminal(events)
    checks.append(terminal_tail_ok)

    pending_approval_ok = _check_pending_approval_not_executing(
        task_id=task_id,
        status=task.status,
        task_registry=task_registry,
    )
    checks.append(pending_approval_ok)

    consistent = all(ok for _, ok, _ in checks)
    return ConsistencyReport(task_id=task_id, consistent=consistent, checks=tuple(checks))


def _check_replay_matches_registry(
    *,
    task_id: str,
    expected: TaskState,
    event_log: EventLog,
) -> tuple[str, bool, str]:
    events = event_log.replay(task_id)
    if not events:
        return ("replay_matches_registry", False, "no events to replay")
    initial_state = events[0].from_state or events[0].to_state
    try:
        reconstructed = event_log.reconstruct(task_id, initial_state)
    except InvalidTransition as exc:
        return ("replay_matches_registry", False, f"replay failed: {exc}")
    if reconstructed is not expected:
        return (
            "replay_matches_registry",
            False,
            f"registry={expected.value}, replay={reconstructed.value}",
        )
    return ("replay_matches_registry", True, "ok")


def _check_execution_events_have_trusted_evidence(
    *,
    task_id: str,
    events: list[EventRecord],
    evidence_registry: EvidenceRegistry,
) -> tuple[str, bool, str]:
    for event in events:
        if event.to_state not in EXECUTION_CLAIMING_STATES:
            continue
        if not event.evidence_refs:
            return (
                "execution_events_have_trusted_evidence",
                False,
                f"seq={event.seq} missing evidence refs for {event.to_state.value}",
            )
        for evidence_id in event.evidence_refs:
            if not evidence_registry.is_trusted(evidence_id):
                return (
                    "execution_events_have_trusted_evidence",
                    False,
                    f"seq={event.seq} untrusted evidence {evidence_id}",
                )
            if evidence_registry.get(evidence_id).task_id != task_id:
                return (
                    "execution_events_have_trusted_evidence",
                    False,
                    f"seq={event.seq} foreign evidence {evidence_id}",
                )
    return ("execution_events_have_trusted_evidence", True, "ok")


def _check_no_events_after_terminal(events: list[EventRecord]) -> tuple[str, bool, str]:
    terminal_seq: int | None = None
    terminal_state: TaskState | None = None
    for event in events:
        if terminal_seq is not None:
            return (
                "terminal_state_has_no_followup_events",
                False,
                f"terminal {terminal_state.value if terminal_state else 'UNKNOWN'} at seq={terminal_seq}",
            )
        if event.to_state in TERMINAL_STATES:
            terminal_seq = event.seq
            terminal_state = event.to_state
    return ("terminal_state_has_no_followup_events", True, "ok")


def _check_pending_approval_not_executing(
    *,
    task_id: str,
    status: TaskState,
    task_registry: TaskRegistry,
) -> tuple[str, bool, str]:
    pending_count = task_registry.session.scalar(
        select(Approval.id)
        .where(Approval.task_id == task_id, Approval.decision == "PENDING")
        .limit(1)
    )
    if pending_count is not None and status in EXECUTION_CLAIMING_STATES:
        return (
            "pending_approval_not_executing",
            False,
            f"status={status.value} with pending approval",
        )
    return ("pending_approval_not_executing", True, "ok")
