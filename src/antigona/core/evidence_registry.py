from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from antigona.models import EvidenceRecord as EvidenceRecordRow
from antigona.models import utcnow


class EvidenceRegistryError(RuntimeError):
    pass


class EvidenceNotFound(EvidenceRegistryError):
    pass


class EvidenceNotTrusted(EvidenceRegistryError):
    pass


class InvalidEvidenceTransition(EvidenceRegistryError):
    pass


class EvidenceStatus(StrEnum):
    PROPOSED = "PROPOSED"
    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class EvidenceKind(StrEnum):
    """Strictly typed evidence contract (Owner, P2 round 3).

    EXECUTION_RESULT is the ONLY kind that may support a DONE transition
    (a terminal successful execution result). APPROVAL_DECISION authorizes
    starting execution but never its completion.
    """

    GENERIC = "GENERIC"
    READINESS = "READINESS"  # pre-execution readiness/plan confirmation
    OBSERVATION = "OBSERVATION"  # runtime observation during execution
    EXECUTION_RESULT = "EXECUTION_RESULT"  # terminal result of an execution
    APPROVAL_DECISION = "APPROVAL_DECISION"  # owner approval (start only)
    ARTIFACT_VERIFICATION = "ARTIFACT_VERIFICATION"


class ExecutionOutcome(StrEnum):
    """Terminality contract of an EXECUTION_RESULT.

    Only SUCCESS may support DONE; FAILED/UNAVAILABLE/PARTIAL are non-terminal
    for the claim and must route the task elsewhere (FAILED/BLOCKED/RETRY...).
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"


class EvidenceSource(StrEnum):
    SOURCE_CODE = "SOURCE_CODE"
    GIT = "GIT"
    TEST_RUNNER = "TEST_RUNNER"
    STATIC_ANALYZER = "STATIC_ANALYZER"
    GATEWAY = "GATEWAY"
    WORKER = "WORKER"
    RUNTIME_PROBE = "RUNTIME_PROBE"
    TELEGRAM_UPDATE = "TELEGRAM_UPDATE"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    OWNER_INPUT = "OWNER_INPUT"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    task_id: str
    attempt_id: str
    type: str
    kind: EvidenceKind
    outcome: ExecutionOutcome | None
    correlation_id: str
    source: EvidenceSource
    created_at: datetime
    sha256: str | None
    artifact_reference: str | None
    status: EvidenceStatus
    supports_claims: tuple[str, ...]
    verified_by: str | None


class EvidenceRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register(
        self,
        *,
        evidence_id: str,
        task_id: str,
        attempt_id: str,
        type: str,
        source: EvidenceSource,
        supports_claims: tuple[str, ...] = (),
        kind: EvidenceKind = EvidenceKind.GENERIC,
        outcome: ExecutionOutcome | None = None,
        correlation_id: str = "",
    ) -> EvidenceRecord:
        if not evidence_id.strip():
            raise EvidenceRegistryError("evidence_id is required")
        if kind is EvidenceKind.EXECUTION_RESULT and outcome is None:
            raise EvidenceRegistryError(
                "EXECUTION_RESULT evidence requires an outcome (SUCCESS/FAILED/UNAVAILABLE/PARTIAL)"
            )
        row = EvidenceRecordRow(
            evidence_id=evidence_id,
            task_id=task_id,
            attempt_id=attempt_id,
            type=type,
            kind=kind.value,
            outcome=outcome.value if outcome is not None else "",
            correlation_id=correlation_id,
            source=source.value,
            created_at=utcnow(),
            sha256=None,
            artifact_reference=None,
            status=EvidenceStatus.PROPOSED.value,
            supports_claims=list(supports_claims),
            verified_by=None,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_record(row)

    def observe(self, evidence_id: str, artifact_reference: str, sha256: str) -> EvidenceRecord:
        row = self._get_row(evidence_id)
        self._transition(row.status, EvidenceStatus.OBSERVED)
        row.artifact_reference = artifact_reference
        row.sha256 = sha256
        row.status = EvidenceStatus.OBSERVED.value
        self.session.flush()
        return self._to_record(row)

    def verify(self, evidence_id: str, *, verified_by: str) -> EvidenceRecord:
        row = self._get_row(evidence_id)
        self._transition(row.status, EvidenceStatus.VERIFIED)
        if EvidenceSource(row.source) is EvidenceSource.MODEL_OUTPUT:
            raise InvalidEvidenceTransition("MODEL_OUTPUT evidence cannot be VERIFIED")
        row.status = EvidenceStatus.VERIFIED.value
        row.verified_by = verified_by
        self.session.flush()
        return self._to_record(row)

    def reject(self, evidence_id: str, reason: str) -> EvidenceRecord:
        if not reason.strip():
            raise EvidenceRegistryError("reason is required")
        row = self._get_row(evidence_id)
        self._transition(row.status, EvidenceStatus.REJECTED)
        row.status = EvidenceStatus.REJECTED.value
        self.session.flush()
        return self._to_record(row)

    def get(self, evidence_id: str) -> EvidenceRecord:
        return self._to_record(self._get_row(evidence_id))

    def for_task(self, task_id: str) -> tuple[EvidenceRecord, ...]:
        rows = self.session.scalars(
            select(EvidenceRecordRow)
            .where(EvidenceRecordRow.task_id == task_id)
            .order_by(EvidenceRecordRow.created_at.asc(), EvidenceRecordRow.evidence_id.asc())
        )
        return tuple(self._to_record(row) for row in rows)

    def is_trusted(self, evidence_id: str) -> bool:
        row = self.session.scalar(
            select(EvidenceRecordRow.status).where(EvidenceRecordRow.evidence_id == evidence_id)
        )
        if row is None:
            return False
        return EvidenceStatus(row) is EvidenceStatus.VERIFIED

    def assert_trusted(self, evidence_id: str) -> None:
        if not self.is_trusted(evidence_id):
            raise EvidenceNotTrusted(evidence_id)

    @staticmethod
    def _transition(from_status: str, to_status: EvidenceStatus) -> None:
        source = EvidenceStatus(from_status)
        if source is to_status:
            return
        if source is EvidenceStatus.PROPOSED and to_status is EvidenceStatus.OBSERVED:
            return
        if source is EvidenceStatus.OBSERVED and to_status is EvidenceStatus.VERIFIED:
            return
        if source in {EvidenceStatus.PROPOSED, EvidenceStatus.OBSERVED} and to_status is EvidenceStatus.REJECTED:
            return
        raise InvalidEvidenceTransition(f"{source.value}->{to_status.value}")

    def _get_row(self, evidence_id: str) -> EvidenceRecordRow:
        row = self.session.scalar(
            select(EvidenceRecordRow).where(EvidenceRecordRow.evidence_id == evidence_id)
        )
        if row is None:
            raise EvidenceNotFound(evidence_id)
        return row

    @staticmethod
    def _to_record(row: EvidenceRecordRow) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row.evidence_id,
            task_id=row.task_id,
            attempt_id=row.attempt_id,
            type=row.type,
            kind=EvidenceKind(row.kind or "GENERIC"),
            outcome=ExecutionOutcome(row.outcome) if row.outcome else None,
            correlation_id=row.correlation_id or "",
            source=EvidenceSource(row.source),
            created_at=row.created_at,
            sha256=row.sha256,
            artifact_reference=row.artifact_reference,
            status=EvidenceStatus(row.status),
            supports_claims=tuple(row.supports_claims),
            verified_by=row.verified_by,
        )

