from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.consistency import verify_task_consistency
from antigona.core.event_log import EventLog
from antigona.core.evidence_registry import (
    EvidenceKind,
    EvidenceRegistry,
    EvidenceSource,
)
from antigona.core.task_registry import TaskRegistry, TaskRegistryError
from antigona.database import Database
from antigona.models import Approval, TaskState


def _base_components(tmp_path: Path, filename: str) -> tuple[Database, TaskRegistry, EventLog, EvidenceRegistry, str]:
    db = Database(f"sqlite:///{tmp_path / filename}")
    db.create_all()
    with db.session_factory() as session:
        task_registry = TaskRegistry(session)
        event_log = EventLog(session)
        evidence_registry = EvidenceRegistry(session)
        created = task_registry.create(
            owner_id="owner-1",
            goal="consistency",
            idempotency_key=f"idem-{filename}",
            correlation_id=f"corr-{filename}-1",
        )
        return db, task_registry, event_log, evidence_registry, created.task_id


def test_report_flags_missing_evidence_for_execution_claiming_event(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'consistency_missing_evidence.db'}")
    db.create_all()

    with db.session_factory() as session:
        task_registry = TaskRegistry(session)
        event_log = EventLog(session)
        evidence_registry = EvidenceRegistry(session)

        created = task_registry.create(
            owner_id="owner-1",
            goal="consistency",
            idempotency_key="idem-missing-evidence",
            correlation_id="corr-ce-1",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-ce-2",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-ce-3",
        )
        evidence_registry.register(
            evidence_id="ev-ce-1",
            task_id=created.task_id,
            attempt_id="a1",
            type="worker_exec",
            kind=EvidenceKind.READINESS,
            source=EvidenceSource.WORKER,
            supports_claims=("exec:run",),
        )
        evidence_registry.observe(
            "ev-ce-1", artifact_reference="logs/1", sha256="0" * 64
        )
        evidence_registry.verify("ev-ce-1", verified_by="verifier")
        task_registry.update_status(
            created.task_id,
            TaskState.TOOL_EXECUTING,
            actor="worker",
            correlation_id="corr-ce-4",
            evidence_refs=("ev-ce-1",),
        )

        # Preventive guard (P2 rework): the registry REFUSES the transition
        # into TOOL_EXECUTING without trusted evidence — the event never lands
        # in the log, so consistency stays consistent (nothing to flag).
        with pytest.raises(TaskRegistryError):
            task_registry.update_status(
                created.task_id,
                TaskState.TOOL_EXECUTING,
                actor="worker",
                correlation_id="corr-ce-4-bad",
            )

        report = verify_task_consistency(
            created.task_id,
            task_registry=task_registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
        )

        assert report.consistent is True
        checks = {name: (ok, detail) for name, ok, detail in report.checks}
        assert checks["execution_events_have_trusted_evidence"][0] is True
        # create + queued + planning + executing(with evidence) = 4; the
        # refused no-evidence attempt never wrote an event.
        assert len(event_log.replay(created.task_id)) == 4


def test_report_flags_terminal_then_followup_event(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'consistency_terminal_followup.db'}")
    db.create_all()

    with db.session_factory() as session:
        task_registry = TaskRegistry(session)
        event_log = EventLog(session)
        evidence_registry = EvidenceRegistry(session)

        created = task_registry.create(
            owner_id="owner-1",
            goal="consistency",
            idempotency_key="idem-terminal-followup",
            correlation_id="corr-tf-1",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-tf-2",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-tf-3",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.WAITING_APPROVAL,
            actor="worker",
            correlation_id="corr-tf-4",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.POLICY_DENIED,
            actor="owner",
            correlation_id="corr-tf-4b",
        )

        event_log.append(
            task_id=created.task_id,
            entity_id=created.task_id,
            entity_type="task",
            from_state=TaskState.POLICY_DENIED,
            to_state=TaskState.QUEUED,
            reason="tampered follow-up",
            actor="tamper",
            correlation_id="corr-tf-5",
        )

        report = verify_task_consistency(
            created.task_id,
            task_registry=task_registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
        )

        checks = {name: (ok, detail) for name, ok, detail in report.checks}
        assert report.consistent is False
        assert checks["terminal_state_has_no_followup_events"][0] is False
        assert "terminal POLICY_DENIED" in checks["terminal_state_has_no_followup_events"][1]


def test_report_flags_pending_approval_with_execution_claiming_status(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'consistency_pending_approval.db'}")
    db.create_all()

    with db.session_factory() as session:
        task_registry = TaskRegistry(session)
        event_log = EventLog(session)
        evidence_registry = EvidenceRegistry(session)

        created = task_registry.create(
            owner_id="owner-1",
            goal="consistency",
            idempotency_key="idem-pending-approval",
            correlation_id="corr-pa-1",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-pa-2",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-pa-3",
        )

        session.add(
            Approval(
                id="synthetic",
                task_id=created.task_id,
                tool_name="workspace.write_text",
                arguments={"path": "x.txt"},
                risk_level="HIGH",
                reason="approved",
                decision="APPROVED",
            )
        )
        session.flush()
        evidence_registry.register(
            evidence_id="ev-approved",
            task_id=created.task_id,
            attempt_id="attempt-1",
            type="approval_decision",
            kind=EvidenceKind.APPROVAL_DECISION,
            source=EvidenceSource.OWNER_INPUT,
            supports_claims=("approval:synthetic",),
        )
        evidence_registry.observe("ev-approved", "approval://synthetic", "f" * 64)
        evidence_registry.verify("ev-approved", verified_by="owner:955_111")

        task_registry.update_status(
            created.task_id,
            TaskState.WAITING_APPROVAL,
            actor="worker",
            correlation_id="corr-pa-3b",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.TOOL_EXECUTING,
            actor="owner:955_111",
            correlation_id="corr-pa-4",
            evidence_refs=("ev-approved",),
        )

        session.add(
            Approval(
                task_id=created.task_id,
                tool_name="workspace.write_text",
                arguments={"path": "y.txt"},
                risk_level="HIGH",
                reason="pending",
                decision="PENDING",
            )
        )
        session.flush()

        report = verify_task_consistency(
            created.task_id,
            task_registry=task_registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
        )

        checks = {name: (ok, detail) for name, ok, detail in report.checks}
        assert report.consistent is False
        assert checks["pending_approval_not_executing"][0] is False
        assert "status=TOOL_EXECUTING" in checks["pending_approval_not_executing"][1]
        assert checks["execution_events_have_trusted_evidence"][0] is True
