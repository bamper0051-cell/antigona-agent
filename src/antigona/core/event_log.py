from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from antigona.durable.state_machine import InvalidTransition, guard_verifying_done, transition
from antigona.models import StateTransition, TaskState

_REASON_ENVELOPE_PREFIX = "EVIDENCE_JSON::"


@dataclass(frozen=True)
class EventRecord:
    seq: int
    task_id: str
    entity_id: str
    entity_type: str
    from_state: TaskState | None
    to_state: TaskState
    reason: str
    actor: str
    correlation_id: str
    created_at: datetime
    evidence_refs: tuple[str, ...]


def _encode_reason(reason: str, evidence_refs: tuple[str, ...]) -> str:
    if not evidence_refs:
        return reason
    payload = {
        "reason": reason,
        "evidence_refs": list(evidence_refs),
    }
    return f"{_REASON_ENVELOPE_PREFIX}{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"


def _decode_reason(reason: str) -> tuple[str, tuple[str, ...]]:
    if not reason.startswith(_REASON_ENVELOPE_PREFIX):
        return reason, ()
    raw_payload = reason[len(_REASON_ENVELOPE_PREFIX) :]
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return reason, ()
    decoded_reason = payload.get("reason")
    decoded_evidence = payload.get("evidence_refs")
    if not isinstance(decoded_reason, str):
        return reason, ()
    if not isinstance(decoded_evidence, list) or not all(
        isinstance(value, str) for value in decoded_evidence
    ):
        return decoded_reason, ()
    return decoded_reason, tuple(decoded_evidence)


class EventLog:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        task_id: str,
        entity_id: str,
        entity_type: str,
        from_state: TaskState | None,
        to_state: TaskState,
        reason: str,
        actor: str,
        correlation_id: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> int:
        if not actor.strip():
            raise ValueError("actor is required")
        if not correlation_id.strip():
            raise ValueError("correlation_id is required")
        existing = self.session.scalar(
            select(StateTransition)
            .where(
                StateTransition.task_id == task_id,
                StateTransition.correlation_id == correlation_id,
            )
            .order_by(StateTransition.id.asc())
            .limit(1)
        )
        if existing is not None:
            # Duplicate resistance: same (task_id, correlation_id) MUST be the
            # same event. If the caller repeats the correlation with different
            # content, that is a stale/malicious replay — fail closed instead
            # of silently masking the real transition.
            expected_to = to_state.value if to_state is not None else None
            current_from = from_state.value if from_state is not None else None
            if (
                existing.to_state != expected_to
                or existing.from_state != current_from
                or existing.actor != actor
            ):
                raise ValueError(
                    f"correlation_id {correlation_id!r} already used for "
                    f"{existing.from_state}->{existing.to_state} by {existing.actor}; "
                    f"refusing replay with {current_from}->{expected_to} by {actor}"
                )
            return existing.id
        row = StateTransition(
            task_id=task_id,
            entity_id=entity_id,
            entity_type=entity_type,
            from_state=from_state.value if from_state is not None else None,
            to_state=to_state.value,
            reason=_encode_reason(reason, evidence_refs),
            actor=actor,
            correlation_id=correlation_id,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def replay(self, task_id: str) -> list[EventRecord]:
        rows = self.session.scalars(
            select(StateTransition)
            .where(StateTransition.task_id == task_id)
            .order_by(StateTransition.id.asc())
        )
        return [self._to_record(row) for row in rows]

    def reconstruct(self, task_id: str, initial_state: TaskState) -> TaskState:
        state = initial_state
        for event in self.replay(task_id):
            if event.from_state is None:
                state = event.to_state
                continue
            if event.from_state is not None and event.from_state is not state:
                raise InvalidTransition(
                    f"event log mismatch at seq={event.seq}: "
                    f"expected from={state.value}, got {event.from_state.value}"
                )
            if event.to_state is TaskState.DONE:
                guard_verifying_done(bool(event.evidence_refs))
            transition(
                state,
                event.to_state,
                cancellation_requested=False,
                verifier_capability=event.actor.startswith("verifier"),
            )
            state = event.to_state
        return state

    def verify_materialized(self, task_id: str, current_state: TaskState) -> bool:
        events = self.replay(task_id)
        if not events:
            return False
        initial_state = events[0].from_state
        if initial_state is None:
            initial_state = events[0].to_state
        reconstructed = self.reconstruct(task_id, initial_state)
        return reconstructed is current_state

    @staticmethod
    def _to_record(row: StateTransition) -> EventRecord:
        decoded_reason, evidence_refs = _decode_reason(row.reason)
        return EventRecord(
            seq=row.id,
            task_id=row.task_id,
            entity_id=row.entity_id,
            entity_type=row.entity_type,
            from_state=TaskState(row.from_state) if row.from_state is not None else None,
            to_state=TaskState(row.to_state),
            reason=decoded_reason,
            actor=row.actor,
            correlation_id=row.correlation_id,
            created_at=row.created_at,
            evidence_refs=evidence_refs,
        )
