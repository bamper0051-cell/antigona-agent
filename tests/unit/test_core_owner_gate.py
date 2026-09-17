from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from antigona.core.event_log import EventLog
from antigona.core.evidence_registry import (
    EvidenceRegistry,
    EvidenceSource,
    EvidenceStatus,
)
from antigona.core.owner_gate import GateDecision, OwnerGate, OwnerGateError, approve_and_continue
from antigona.core.task_registry import TaskRegistry, TaskRegistryError
from antigona.database import Database
from antigona.models import Approval, TaskState


def _setup(tmp_path: Path, name: str) -> tuple[Database, TaskRegistry, EventLog, EvidenceRegistry, OwnerGate, str]:
    db = Database(f"sqlite:///{tmp_path / name}")
    db.create_all()
    session = db.session_factory()
    registry = TaskRegistry(session)
    event_log = EventLog(session)
    evidence_registry = EvidenceRegistry(session)
    owner_gate = OwnerGate(session, evidence_registry=evidence_registry)
    created = registry.create(
        owner_id="owner-1",
        goal="goal",
        idempotency_key=f"idem-{name}",
        correlation_id=f"corr-{name}-1",
    )
    registry.update_status(
        created.task_id,
        TaskState.QUEUED,
        actor="gateway",
        correlation_id=f"corr-{name}-2",
    )
    registry.update_status(
        created.task_id,
        TaskState.PLANNING,
        actor="worker",
        correlation_id=f"corr-{name}-3",
    )
    registry.update_status(
        created.task_id,
        TaskState.WAITING_APPROVAL,
        actor="worker",
        correlation_id=f"corr-{name}-4",
    )
    return db, registry, event_log, evidence_registry, owner_gate, created.task_id


def test_open_decide_approved_registers_verified_evidence_and_transitions(tmp_path: Path) -> None:
    _db, registry, event_log, evidence_registry, owner_gate, task_id = _setup(
        tmp_path, "owner_gate_approve.db"
    )

    approval_id = owner_gate.open_approval(
        task_id=task_id,
        tool_name="workspace.write_text",
        arguments={"path": "notes.txt"},
        risk_level="HIGH",
        reason="requires owner confirmation",
        correlation_id="corr-open-1",
    )

    status = approve_and_continue(
        approval_id,
        owner_user_id=955_111,
        task_registry=registry,
        event_log=event_log,
        evidence_registry=evidence_registry,
        correlation_id="corr-open-2",
    )

    evidence = owner_gate.evidence_for(approval_id)
    replay = event_log.replay(task_id)

    assert status.status is TaskState.TOOL_EXECUTING
    assert evidence is not None
    assert evidence.status is EvidenceStatus.VERIFIED
    assert evidence.source is EvidenceSource.OWNER_INPUT
    assert evidence_registry.is_trusted(evidence.evidence_id)
    assert f"approval:{approval_id}" in evidence.supports_claims
    assert replay[-1].to_state is TaskState.TOOL_EXECUTING
    assert replay[-1].evidence_refs == (evidence.evidence_id,)


def test_deny_path_transitions_to_policy_denied(tmp_path: Path) -> None:
    _db, registry, event_log, evidence_registry, owner_gate, task_id = _setup(
        tmp_path, "owner_gate_deny.db"
    )

    approval_id = owner_gate.open_approval(
        task_id=task_id,
        tool_name="workspace.write_text",
        arguments={"path": "notes.txt"},
        risk_level="HIGH",
        reason="requires owner confirmation",
        correlation_id="corr-deny-1",
    )

    updated = approve_and_continue(
        approval_id,
        owner_user_id=955_111,
        task_registry=registry,
        event_log=event_log,
        evidence_registry=evidence_registry,
        correlation_id="corr-deny-2",
        decision=False,
    )

    assert updated.status is TaskState.POLICY_DENIED
    assert owner_gate.evidence_for(approval_id) is None


def test_decide_is_idempotent_after_first_decision(tmp_path: Path) -> None:
    _db, _registry, _event_log, _evidence_registry, owner_gate, task_id = _setup(
        tmp_path, "owner_gate_idempotent.db"
    )

    approval_id = owner_gate.open_approval(
        task_id=task_id,
        tool_name="workspace.write_text",
        arguments={"path": "notes.txt"},
        risk_level="HIGH",
        reason="requires owner confirmation",
        correlation_id="corr-idemp-1",
    )

    first = owner_gate.decide(
        approval_id,
        owner_user_id=955_111,
        decision=True,
        correlation_id="corr-idemp-2",
    )
    approval_row = owner_gate.session.scalar(select(Approval).where(Approval.id == approval_id))
    assert approval_row is not None
    first_decided_at = approval_row.decided_at
    first_decided_by = approval_row.decided_by

    second = owner_gate.decide(
        approval_id,
        owner_user_id=42,
        decision=False,
        correlation_id="corr-idemp-3",
    )
    approval_row_after = owner_gate.session.scalar(select(Approval).where(Approval.id == approval_id))
    assert approval_row_after is not None

    assert first is GateDecision.APPROVED
    assert second is GateDecision.APPROVED
    assert approval_row_after.decided_at == first_decided_at
    assert approval_row_after.decided_by == first_decided_by


def test_fail_closed_unknown_missing_owner_missing_correlation_and_already_decided(tmp_path: Path) -> None:
    _db, registry, event_log, evidence_registry, owner_gate, task_id = _setup(
        tmp_path, "owner_gate_fail_closed.db"
    )

    with pytest.raises(OwnerGateError):
        owner_gate.decide(
            "missing-approval",
            owner_user_id=955_111,
            decision=True,
            correlation_id="corr-missing",
        )

    approval_id = owner_gate.open_approval(
        task_id=task_id,
        tool_name="workspace.write_text",
        arguments={"path": "notes.txt"},
        risk_level="HIGH",
        reason="requires owner confirmation",
        correlation_id="corr-fc-1",
    )

    with pytest.raises(OwnerGateError):
        owner_gate.decide(
            approval_id,
            owner_user_id=0,
            decision=True,
            correlation_id="corr-fc-2",
        )

    with pytest.raises(OwnerGateError):
        owner_gate.decide(
            approval_id,
            owner_user_id=955_111,
            decision=True,
            correlation_id="",
        )

    owner_gate.decide(
        approval_id,
        owner_user_id=955_111,
        decision=True,
        correlation_id="corr-fc-3",
    )

    with pytest.raises(OwnerGateError):
        approve_and_continue(
            approval_id,
            owner_user_id=955_111,
            task_registry=registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
            correlation_id="corr-fc-4",
        )


def test_owner_gate_bypass_waiting_approval_to_tool_executing_requires_trusted_approval_evidence(
    tmp_path: Path,
) -> None:
    _db, registry, _event_log, evidence_registry, _owner_gate, task_id = _setup(
        tmp_path, "owner_gate_bypass.db"
    )

    with pytest.raises(TaskRegistryError):
        registry.update_status(
            task_id,
            TaskState.TOOL_EXECUTING,
            actor="worker",
            correlation_id="corr-bypass-1",
        )

    evidence_registry.register(
        evidence_id="ev-untrusted",
        task_id=task_id,
        attempt_id="attempt-1",
        type="approval_decision",
        source=EvidenceSource.OWNER_INPUT,
        supports_claims=("approval:abc",),
    )

    with pytest.raises(TaskRegistryError):
        registry.update_status(
            task_id,
            TaskState.TOOL_EXECUTING,
            actor="worker",
            correlation_id="corr-bypass-2",
            evidence_refs=("ev-untrusted",),
        )

    evidence_registry.observe("ev-untrusted", "approval://abc", "a" * 64)
    evidence_registry.verify("ev-untrusted", verified_by="owner:955_111")

    evidence_registry.register(
        evidence_id="ev-non-approval",
        task_id=task_id,
        attempt_id="attempt-2",
        type="owner_comment",
        source=EvidenceSource.OWNER_INPUT,
        supports_claims=("note:ok",),
    )
    evidence_registry.observe("ev-non-approval", "approval://note", "b" * 64)
    evidence_registry.verify("ev-non-approval", verified_by="owner:955_111")

    with pytest.raises(TaskRegistryError):
        registry.update_status(
            task_id,
            TaskState.TOOL_EXECUTING,
            actor="worker",
            correlation_id="corr-bypass-3",
            evidence_refs=("ev-non-approval",),
        )


def test_done_requires_trusted_evidence_refs(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'owner_gate_done_trusted.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-done",
            correlation_id="corr-done-1",
        )
        registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-done-2",
        )
        registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-done-3",
        )
        from antigona.core.evidence_registry import (
    EvidenceKind,
    EvidenceRegistry,
    EvidenceSource,
    ExecutionOutcome,
)

        evidence_registry = EvidenceRegistry(session)
        evidence_registry.register(
            evidence_id="ev-done-1",
            task_id=created.task_id,
            attempt_id="a1",
            type="worker_exec",
            kind=EvidenceKind.EXECUTION_RESULT,
            outcome=ExecutionOutcome.SUCCESS,
            correlation_id="corr-done-4",
            source=EvidenceSource.WORKER,
            supports_claims=("exec:run",),
        )
        evidence_registry.observe(
            "ev-done-1", artifact_reference="logs/1", sha256="0" * 64
        )
        evidence_registry.verify("ev-done-1", verified_by="verifier")
        registry.update_status(
            created.task_id,
            TaskState.TOOL_EXECUTING,
            actor="worker",
            correlation_id="corr-done-4",
            evidence_refs=("ev-done-1",),
        )
        registry.update_status(
            created.task_id,
            TaskState.OBSERVING,
            actor="worker",
            correlation_id="corr-done-5",
            evidence_refs=("ev-done-1",),
        )
        registry.update_status(
            created.task_id,
            TaskState.VERIFYING,
            actor="worker",
            correlation_id="corr-done-6",
            evidence_refs=("ev-done-1",),
        )

        with pytest.raises(TaskRegistryError):
            registry.update_status(
                created.task_id,
                TaskState.DONE,
                actor="verifier",
                correlation_id="corr-done-7",
                evidence_refs=("missing-evidence",),
                verifier_capability=True,
            )
