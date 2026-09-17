from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from antigona.core.event_log import EventLog
from antigona.core.evidence_registry import (
    EvidenceKind,
    EvidenceNotFound,
    EvidenceRecord,
    EvidenceRegistry,
    EvidenceSource,
)
from antigona.core.task_registry import TaskRecord, TaskRegistry
from antigona.models import Approval, TaskState, utcnow


class OwnerGateError(RuntimeError):
    pass


class GateDecision(StrEnum):
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class OwnerGate:
    def __init__(self, session: Session, *, evidence_registry: EvidenceRegistry | None = None) -> None:
        self.session = session
        self.evidence_registry = evidence_registry or EvidenceRegistry(session)

    def open_approval(
        self,
        task_id: str,
        tool_name: str,
        arguments: dict[str, object],
        risk_level: str,
        reason: str,
        correlation_id: str,
    ) -> str:
        if not task_id.strip():
            raise OwnerGateError("task_id is required")
        if not tool_name.strip():
            raise OwnerGateError("tool_name is required")
        if not risk_level.strip():
            raise OwnerGateError("risk_level is required")
        if not reason.strip():
            raise OwnerGateError("reason is required")
        if not correlation_id.strip():
            raise OwnerGateError("correlation_id is required")

        row = Approval(
            task_id=task_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            reason=reason,
            decision="PENDING",
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def decide(
        self,
        approval_id: str,
        *,
        owner_user_id: int,
        decision: bool,
        correlation_id: str,
    ) -> GateDecision:
        if not approval_id.strip():
            raise OwnerGateError("approval_id is required")
        if owner_user_id <= 0:
            raise OwnerGateError("owner_user_id is required")
        if not correlation_id.strip():
            raise OwnerGateError("correlation_id is required")

        approval = self._load_approval(approval_id)
        current = self._to_gate_decision(approval.decision)
        if current is not None and current is not GateDecision.EXPIRED:
            return current

        if approval.decision != "PENDING":
            return GateDecision.EXPIRED

        # Compare-and-set: only a PENDING row may be flipped. Atomic UPDATE
        # with a PENDING guard defeats cross-process TOCTOU (two gateways
        # racing decide()). If the row was already decided by a concurrent
        # writer, rowcount == 0 and we return the stored decision instead.
        new_decision = GateDecision.APPROVED.value if decision else GateDecision.DENIED.value
        result: CursorResult[Any] = cast(
            CursorResult[Any],
            self.session.execute(
                update(Approval)
                .where(Approval.id == approval_id, Approval.decision == "PENDING")
                .values(
                    decision=new_decision,
                    decided_by=str(owner_user_id),
                    decided_at=utcnow(),
                )
            ),
        )
        if result.rowcount == 0:
            self.session.flush()
            self.session.refresh(approval)
            dec = self._to_gate_decision(approval.decision)
            return dec if dec is not None else GateDecision.EXPIRED

        approval.decision = new_decision
        approval.decided_by = str(owner_user_id)
        approval.decided_at = utcnow()
        self.session.flush()
        return GateDecision(new_decision)

    def evidence_for(self, approval_id: str) -> EvidenceRecord | None:
        approval = self._load_approval(approval_id)
        if approval.decision != GateDecision.APPROVED.value:
            return None

        evidence_id = _approval_evidence_id(approval.id)
        claim = f"approval:{approval.id}"

        try:
            existing = self.evidence_registry.get(evidence_id)
        except EvidenceNotFound:
            existing = None

        if existing is not None:
            return existing

        registered = self.evidence_registry.register(
            evidence_id=evidence_id,
            task_id=approval.task_id,
            attempt_id=approval.id,
            type="approval_decision",
            kind=EvidenceKind.APPROVAL_DECISION,
            correlation_id=approval.id,
            source=EvidenceSource.OWNER_INPUT,
            supports_claims=(claim,),
        )
        observed = self.evidence_registry.observe(
            registered.evidence_id,
            artifact_reference=f"approval://{approval.id}",
            sha256=hashlib.sha256(
                f"{approval.id}:{approval.decided_by}:{approval.decided_at}".encode()
            ).hexdigest(),
        )
        verified_by = f"owner:{approval.decided_by}" if approval.decided_by else "owner"
        return self.evidence_registry.verify(observed.evidence_id, verified_by=verified_by)

    def get(self, approval_id: str) -> Approval:
        return self._load_approval(approval_id)

    def _load_approval(self, approval_id: str) -> Approval:
        approval = self.session.scalar(select(Approval).where(Approval.id == approval_id))
        if approval is None:
            raise OwnerGateError("unknown approval_id")
        return approval

    @staticmethod
    def _to_gate_decision(value: str) -> GateDecision | None:
        if value == "PENDING":
            return None
        try:
            return GateDecision(value)
        except ValueError:
            return GateDecision.EXPIRED


def approve_and_continue(
    approval_id: str,
    *,
    owner_user_id: int,
    task_registry: TaskRegistry,
    event_log: EventLog,
    evidence_registry: EvidenceRegistry,
    correlation_id: str,
    decision: bool = True,
) -> TaskRecord:
    if owner_user_id <= 0:
        raise OwnerGateError("owner_user_id is required")
    if not correlation_id.strip():
        raise OwnerGateError("correlation_id is required")
    if task_registry.session is not event_log.session:
        raise OwnerGateError("task_registry and event_log must share one session")

    owner_gate = OwnerGate(task_registry.session, evidence_registry=evidence_registry)
    approval = owner_gate.get(approval_id)
    if approval.decision != "PENDING":
        raise OwnerGateError("approval already decided")

    gate_decision = owner_gate.decide(
        approval.id,
        owner_user_id=owner_user_id,
        decision=decision,
        correlation_id=correlation_id,
    )
    actor = f"owner:{owner_user_id}"
    if gate_decision is GateDecision.DENIED:
        return task_registry.update_status(
            approval.task_id,
            TaskState.POLICY_DENIED,
            actor=actor,
            correlation_id=correlation_id,
        )

    evidence = owner_gate.evidence_for(approval.id)
    if evidence is None:
        raise OwnerGateError("approved decision requires evidence")

    return task_registry.update_status(
        approval.task_id,
        TaskState.TOOL_EXECUTING,
        actor=actor,
        correlation_id=correlation_id,
        evidence_refs=(evidence.evidence_id,),
    )


def _approval_evidence_id(approval_id: str) -> str:
    return f"approval-evidence:{approval_id}"
