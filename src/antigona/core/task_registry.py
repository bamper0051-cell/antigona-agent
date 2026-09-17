from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import CursorResult, Select, select, update
from sqlalchemy.orm import Session

from antigona.core.event_log import EventLog
from antigona.core.evidence_registry import (
    EvidenceKind,
    EvidenceSource,
    EvidenceStatus,
    ExecutionOutcome,
)
from antigona.durable.state_machine import (
    ConcurrentUpdate,
    guard_verifying_done,
    transition,
)
from antigona.models import Approval, StateTransition, TaskFlow, TaskState, utcnow
from antigona.models import EvidenceRecord as EvidenceRecordRow


class TaskRegistryError(RuntimeError):
    pass


class TaskRegistryNotFound(TaskRegistryError):
    pass


_APPROVAL_CLAIM_PREFIX = "approval:"

#: Sources whose VERIFIED evidence may support execution claims. MODEL_OUTPUT
#: is deliberately excluded — LLM output/trace/plan/summary can never back an
#: execution claim (P0 invariant, enforced here in depth on top of the
#: evidence_registry hard rule).
_EXECUTION_ALLOWED_SOURCES: frozenset[EvidenceSource] = frozenset(
    {
        EvidenceSource.OWNER_INPUT,
        EvidenceSource.GATEWAY,
        EvidenceSource.WORKER,
        EvidenceSource.RUNTIME_PROBE,
        EvidenceSource.TELEGRAM_UPDATE,
        EvidenceSource.SOURCE_CODE,
        EvidenceSource.GIT,
        EvidenceSource.TEST_RUNNER,
        EvidenceSource.STATIC_ANALYZER,
    }
)

# ── Transition-specific evidence policy (Owner, P2 round 3) ────────────────
#: Execution-authoritative sources — the only sources that can prove a real
#: execution happened and finished (GATEWAY/WORKER/RUNTIME_PROBE).
_EXECUTION_AUTHORITATIVE_SOURCES: frozenset[EvidenceSource] = frozenset(
    {
        EvidenceSource.GATEWAY,
        EvidenceSource.WORKER,
        EvidenceSource.RUNTIME_PROBE,
    }
)

#: DONE requires a terminal successful EXECUTION_RESULT from an
#: execution-authoritative source, bound to the operation correlation.
_DONE_KINDS: frozenset[EvidenceKind] = frozenset({EvidenceKind.EXECUTION_RESULT})
_DONE_SOURCES: frozenset[EvidenceSource] = _EXECUTION_AUTHORITATIVE_SOURCES
_DONE_OUTCOME: ExecutionOutcome = ExecutionOutcome.SUCCESS

#: WAITING_APPROVAL -> TOOL_EXECUTING requires an owner approval decision.
_APPROVAL_EXEC_KINDS: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.APPROVAL_DECISION}
)
_APPROVAL_EXEC_SOURCES: frozenset[EvidenceSource] = frozenset(
    {EvidenceSource.OWNER_INPUT}
)

#: Non-approval entry into TOOL_EXECUTING (READY/PLANNING/PAUSED/RETRY…):
#: readiness or an execution result from a pre-execution authority.
_NONAPPROVAL_EXEC_KINDS: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.READINESS, EvidenceKind.EXECUTION_RESULT}
)
_NONAPPROVAL_EXEC_SOURCES: frozenset[EvidenceSource] = frozenset(
    {
        EvidenceSource.GATEWAY,
        EvidenceSource.WORKER,
        EvidenceSource.RUNTIME_PROBE,
        EvidenceSource.TEST_RUNNER,
        EvidenceSource.STATIC_ANALYZER,
    }
)

#: OBSERVING / VERIFYING: runtime observation or an execution result from an
#: execution-authoritative source (any outcome — observation/verification may
#: legitimately see partial/failed states).
_OBSERVING_VERIFYING_KINDS: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.OBSERVATION, EvidenceKind.EXECUTION_RESULT}
)
_OBSERVING_VERIFYING_SOURCES: frozenset[EvidenceSource] = (
    _EXECUTION_AUTHORITATIVE_SOURCES
)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner_id: str
    goal: str
    status: TaskState
    revision: int
    cancellation_requested: bool
    idempotency_key: str
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    parent_id: str | None
    depth: int


@dataclass(frozen=True)
class TaskListFilter:
    owner_id: str | None = None
    status: TaskState | None = None
    parent_id: str | None = None
    limit: int = 100
    offset: int = 0


class TaskRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._event_log = EventLog(session)

    def create(
        self,
        *,
        owner_id: str,
        goal: str,
        idempotency_key: str,
        correlation_id: str,
        target_path: str = ".",
        content: str = "",
        tool_name: str = "workspace.write_text",
        tool_arguments: dict[str, object] | None = None,
        parent_id: str | None = None,
        depth: int = 0,
        status: TaskState = TaskState.RECEIVED,
    ) -> TaskRecord:
        existing = self.session.scalar(
            select(TaskFlow).where(
                TaskFlow.owner_id == owner_id,
                TaskFlow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return self._to_record(existing)

        row = TaskFlow(
            owner_id=owner_id,
            goal=goal,
            target_path=target_path,
            content=content,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            tool_arguments=tool_arguments or {},
            status=status.value,
            parent_id=parent_id,
            depth=depth,
        )
        self.session.add(row)
        self.session.flush()
        self._event_log.append(
            task_id=row.id,
            entity_id=row.id,
            entity_type="task",
            from_state=None,
            to_state=status,
            reason="task created",
            actor="registry",
            correlation_id=correlation_id,
        )
        self.session.flush()
        return self._to_record(row)

    def get(self, task_id: str) -> TaskRecord:
        row = self.session.scalar(select(TaskFlow).where(TaskFlow.id == task_id))
        if row is None:
            raise TaskRegistryNotFound(task_id)
        return self._to_record(row)

    def list(self, filters: TaskListFilter | None = None) -> list[TaskRecord]:
        effective_filters = filters or TaskListFilter()
        stmt: Select[tuple[TaskFlow]] = select(TaskFlow)
        if effective_filters.owner_id is not None:
            stmt = stmt.where(TaskFlow.owner_id == effective_filters.owner_id)
        if effective_filters.status is not None:
            stmt = stmt.where(TaskFlow.status == effective_filters.status.value)
        if effective_filters.parent_id is not None:
            stmt = stmt.where(TaskFlow.parent_id == effective_filters.parent_id)
        rows = self.session.scalars(
            stmt.order_by(TaskFlow.created_at.asc())
            .offset(max(0, effective_filters.offset))
            .limit(max(1, effective_filters.limit))
        )
        return [self._to_record(row) for row in rows]

    def update_status(
        self,
        task_id: str,
        target: TaskState,
        *,
        actor: str,
        correlation_id: str,
        evidence_refs: tuple[str, ...] = (),
        verifier_capability: bool = False,
    ) -> TaskRecord:
        if not actor.strip():
            raise TaskRegistryError("actor is required")
        if not correlation_id.strip():
            raise TaskRegistryError("correlation_id is required")

        row = self.session.scalar(select(TaskFlow).where(TaskFlow.id == task_id))
        if row is None:
            raise TaskRegistryNotFound(task_id)

        current = TaskState(row.status)
        self._assert_evidence_requirements(
            task_id=task_id,
            current=current,
            target=target,
            evidence_refs=evidence_refs,
            correlation_id=correlation_id,
        )
        if target is TaskState.DONE:
            guard_verifying_done(bool(evidence_refs))
        transition(
            current,
            target,
            cancellation_requested=row.cancellation_requested,
            verifier_capability=verifier_capability,
        )

        expected_revision = row.revision
        updated_at = utcnow()
        result = self.session.execute(
            update(TaskFlow)
            .where(TaskFlow.id == task_id, TaskFlow.revision == expected_revision)
            .values(status=target.value, revision=expected_revision + 1, updated_at=updated_at)
        )
        assert isinstance(result, CursorResult)
        if result.rowcount != 1:
            raise ConcurrentUpdate("task revision CAS failed")

        row.status = target.value
        row.revision = expected_revision + 1
        row.updated_at = updated_at
        self._event_log.append(
            task_id=row.id,
            entity_id=row.id,
            entity_type="task",
            from_state=current,
            to_state=target,
            reason=f"status updated to {target.value}",
            actor=actor,
            correlation_id=correlation_id,
            evidence_refs=evidence_refs,
        )
        self.session.flush()
        return self._to_record(row)

    def _assert_evidence_requirements(
        self,
        *,
        task_id: str,
        current: TaskState,
        target: TaskState,
        evidence_refs: tuple[str, ...],
        correlation_id: str,
    ) -> None:
        """Preventive execution-claim guard (fail-closed, BEFORE CAS + append).

        Entering ANY execution-claiming state (TOOL_EXECUTING, OBSERVING,
        VERIFYING, DONE) requires trusted evidence; the exact kind/source/
        outcome contract is transition-specific:

          * TOOL_EXECUTING (from WAITING_APPROVAL): APPROVAL_DECISION from
            OWNER_INPUT, claim resolvable to an APPROVED Approval of this task.
          * TOOL_EXECUTING (other sources): READINESS or EXECUTION_RESULT from
            a pre-execution authority (GATEWAY/WORKER/RUNTIME_PROBE/
            TEST_RUNNER/STATIC_ANALYZER).
          * OBSERVING / VERIFYING: OBSERVATION or EXECUTION_RESULT from an
            execution-authoritative source (any outcome).
          * DONE: EXECUTION_RESULT with outcome SUCCESS from an
            execution-authoritative source (GATEWAY/WORKER/RUNTIME_PROBE),
            bound to THIS operation via correlation_id == transition
            correlation_id. OWNER_INPUT/TELEGRAM_UPDATE/SOURCE_CODE/GIT/
            TEST_RUNNER/STATIC_ANALYZER/MODEL_OUTPUT can NEVER back DONE.
        """
        from antigona.durable.state_machine import EXECUTION_CLAIMING_STATES

        if target not in EXECUTION_CLAIMING_STATES:
            return

        if not evidence_refs:
            raise TaskRegistryError(
                f"{current.value} -> {target.value} requires verified evidence_refs"
            )

        if len(set(evidence_refs)) != len(evidence_refs):
            raise TaskRegistryError("duplicated evidence_refs are not allowed")

        rows = self._evidence_rows(task_id=task_id, evidence_refs=evidence_refs)
        if len(rows) != len(set(evidence_refs)):
            raise TaskRegistryError(
                "some evidence_refs are unknown or belong to another task"
            )
        for row in rows:
            if row.status != EvidenceStatus.VERIFIED.value:
                raise TaskRegistryError(
                    f"evidence {row.evidence_id} is {row.status}, VERIFIED required"
                )
            source = EvidenceSource(row.source)
            if source not in _EXECUTION_ALLOWED_SOURCES:
                raise TaskRegistryError(
                    f"evidence {row.evidence_id} source {source.value} is not allowed "
                    "for execution claims"
                )

        if target is TaskState.DONE:
            self._assert_done_evidence(rows=rows, correlation_id=correlation_id)
            return
        if target is TaskState.TOOL_EXECUTING and current is TaskState.WAITING_APPROVAL:
            self._assert_approval_evidence(rows=rows)
            return
        if target is TaskState.TOOL_EXECUTING:
            self._assert_policy(
                rows=rows,
                kinds=_NONAPPROVAL_EXEC_KINDS,
                sources=_NONAPPROVAL_EXEC_SOURCES,
                label="TOOL_EXECUTING",
            )
            return
        # OBSERVING / VERIFYING
        self._assert_policy(
            rows=rows,
            kinds=_OBSERVING_VERIFYING_KINDS,
            sources=_OBSERVING_VERIFYING_SOURCES,
            label=target.value,
        )

    def _assert_done_evidence(
        self, *, rows: tuple[EvidenceRecordRow, ...], correlation_id: str
    ) -> None:
        """VERIFYING -> DONE: terminal successful EXECUTION_RESULT bound to the
        operation correlation, from an execution-authoritative source."""
        if not rows:
            raise TaskRegistryError("DONE requires verified evidence_refs")
        for row in rows:
            kind = EvidenceKind(row.kind or "GENERIC")
            if kind not in _DONE_KINDS:
                raise TaskRegistryError(
                    f"DONE requires evidence kind EXECUTION_RESULT, got {kind.value}"
                )
            source = EvidenceSource(row.source)
            if source not in _DONE_SOURCES:
                raise TaskRegistryError(
                    f"DONE requires execution-authoritative source "
                    f"(GATEWAY/WORKER/RUNTIME_PROBE), got {source.value}"
                )
            outcome = ExecutionOutcome(row.outcome) if row.outcome else None
            if outcome is not _DONE_OUTCOME:
                raise TaskRegistryError(
                    f"DONE requires terminal SUCCESS outcome, got {outcome.value if outcome else 'none'}"
                )
            if row.correlation_id != correlation_id:
                raise TaskRegistryError(
                    f"DONE evidence {row.evidence_id} correlation "
                    f"{row.correlation_id!r} does not match operation correlation "
                    f"{correlation_id!r}"
                )

    def _assert_approval_evidence(self, *, rows: tuple[EvidenceRecordRow, ...]) -> None:
        """WAITING_APPROVAL -> TOOL_EXECUTING requires a resolvable APPROVED
        Approval of THIS task (adversarial F1/F2 fix)."""
        for row in rows:
            kind = EvidenceKind(row.kind or "GENERIC")
            if kind not in _APPROVAL_EXEC_KINDS:
                raise TaskRegistryError(
                    "WAITING_APPROVAL -> TOOL_EXECUTING requires evidence kind "
                    f"APPROVAL_DECISION, got {kind.value}"
                )
            source = EvidenceSource(row.source)
            if source not in _APPROVAL_EXEC_SOURCES:
                raise TaskRegistryError(
                    "WAITING_APPROVAL -> TOOL_EXECUTING requires OWNER_INPUT "
                    f"approval evidence, got {source.value}"
                )
            supports_claims = tuple(str(claim) for claim in row.supports_claims)
            for claim in supports_claims:
                if not claim.startswith(_APPROVAL_CLAIM_PREFIX):
                    continue
                approval_id = claim[len(_APPROVAL_CLAIM_PREFIX):]
                if not approval_id:
                    continue
                approval = self.session.get(Approval, approval_id)
                if (
                    approval is not None
                    and approval.task_id == row.task_id
                    and approval.decision == "APPROVED"
                ):
                    return
        raise TaskRegistryError(
            "WAITING_APPROVAL -> TOOL_EXECUTING requires a trusted OWNER_INPUT "
            "approval claim for this task"
        )

    def _assert_policy(
        self,
        *,
        rows: tuple[EvidenceRecordRow, ...],
        kinds: frozenset[EvidenceKind],
        sources: frozenset[EvidenceSource],
        label: str,
    ) -> None:
        for row in rows:
            kind = EvidenceKind(row.kind or "GENERIC")
            if kind not in kinds:
                raise TaskRegistryError(
                    f"{label} does not accept evidence kind {kind.value}"
                )
            source = EvidenceSource(row.source)
            if source not in sources:
                raise TaskRegistryError(
                    f"{label} does not accept evidence source {source.value}"
                )

    def _all_refs_are_trusted(self, *, task_id: str, evidence_refs: tuple[str, ...]) -> bool:
        rows = self._evidence_rows(task_id=task_id, evidence_refs=evidence_refs)
        return len(rows) == len(set(evidence_refs)) and all(
            row.status == EvidenceStatus.VERIFIED.value for row in rows
        )

    def _evidence_rows(self, *, task_id: str, evidence_refs: tuple[str, ...]) -> tuple[EvidenceRecordRow, ...]:
        if not evidence_refs:
            return ()
        rows = self.session.scalars(
            select(EvidenceRecordRow).where(
                EvidenceRecordRow.task_id == task_id,
                EvidenceRecordRow.evidence_id.in_(set(evidence_refs)),
            )
        )
        return tuple(rows)

    def _to_record(self, row: TaskFlow) -> TaskRecord:
        correlation_id = self.session.scalar(
            select(StateTransition.correlation_id)
            .where(StateTransition.task_id == row.id)
            .order_by(StateTransition.id.asc())
            .limit(1)
        )
        return TaskRecord(
            task_id=row.id,
            owner_id=row.owner_id,
            goal=row.goal,
            status=TaskState(row.status),
            revision=row.revision,
            cancellation_requested=row.cancellation_requested,
            idempotency_key=row.idempotency_key,
            correlation_id=correlation_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            lease_owner=row.lease_owner,
            lease_expires_at=row.lease_expires_at,
            parent_id=row.parent_id,
            depth=row.depth,
        )
