from __future__ import annotations

from pathlib import Path

from antigona.core.consistency import verify_task_consistency
from antigona.core.event_log import EventLog
from antigona.core.evidence_registry import EvidenceRegistry
from antigona.core.owner_gate import OwnerGate, approve_and_continue
from antigona.core.task_registry import TaskRegistry
from antigona.database import Database
from antigona.models import TaskState


def test_owner_gate_flow_journal_replay_and_consistency(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'owner_gate_flow.db'}")
    db.create_all()

    with db.session_factory() as session:
        task_registry = TaskRegistry(session)
        event_log = EventLog(session)
        evidence_registry = EvidenceRegistry(session)
        owner_gate = OwnerGate(session, evidence_registry=evidence_registry)

        created = task_registry.create(
            owner_id="owner-1",
            goal="apply deterministic owner gate",
            idempotency_key="idem-owner-gate",
            correlation_id="corr-og-1",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-og-2",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-og-3",
        )
        task_registry.update_status(
            created.task_id,
            TaskState.WAITING_APPROVAL,
            actor="worker",
            correlation_id="corr-og-4",
        )

        approval_id = owner_gate.open_approval(
            task_id=created.task_id,
            tool_name="workspace.write_text",
            arguments={"path": "report.txt", "content": "ok"},
            risk_level="HIGH",
            reason="requires owner approval",
            correlation_id="corr-og-5",
        )

        transitioned = approve_and_continue(
            approval_id,
            owner_user_id=955_111,
            task_registry=task_registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
            correlation_id="corr-og-6",
        )
        session.commit()

        replay = event_log.replay(created.task_id)
        reconstructed = event_log.reconstruct(created.task_id, TaskState.RECEIVED)
        report = verify_task_consistency(
            created.task_id,
            task_registry=task_registry,
            event_log=event_log,
            evidence_registry=evidence_registry,
        )

        assert transitioned.status is TaskState.TOOL_EXECUTING
        assert replay[-1].to_state is TaskState.TOOL_EXECUTING
        assert replay[-1].evidence_refs
        assert reconstructed is TaskState.TOOL_EXECUTING
        assert report.consistent is True
        assert all(ok for _, ok, _ in report.checks)
