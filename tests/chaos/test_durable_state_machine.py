"""P0.3 chaos suite: crash recovery, sticky cancel, illegal-transition audit.

These exercises drive the durable state machine through the failure modes the
roadmap checkpoint names: a worker killed mid-step, a cancel that must outlive a
restart, an illegal transition that must be refused *and* journalled, and an
outbox that must deliver exactly once even when a sender crashes mid-flight.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.contracts import ToolResult, WriteFileInput
from antigona.database import Database
from antigona.delivery import DeliveryWorker, FakeAdapter
from antigona.durable import RecoveryWorker
from antigona.durable.state_machine import InvalidTransition, check_task_transition
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import (
    DeliveryOutbox,
    QueueJob,
    StateTransition,
    StepState,
    TaskFlow,
    TaskState,
    utcnow,
)
from antigona.orchestrator import Orchestrator
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier_service import create_verifier_app


class CountingTool(WorkspaceFileTool):
    calls = 0

    def execute(self, arguments: WriteFileInput) -> ToolResult:
        self.calls += 1
        return super().execute(arguments)


class VerifierHarness:
    def __init__(self, url: str, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
        self.url = url
        self.client = TestClient(create_verifier_app(url, "secret", deterministic_test_judge()))
        self.client.__enter__()

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        seed_private_criteria(self.url, task_id)
        response = self.client.post(
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        response.raise_for_status()
        return str(response.json()["decision"])


def _make(tmp_path: Path) -> tuple[Database, CountingTool, str]:
    db = Database(f"sqlite:///{tmp_path/'db'}")
    db.create_all()
    tool = CountingTool(InProcessTestBackend(tmp_path / "w", test_mode=True))
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("o", "g", "target.txt", "BODY", "k"))
        repo.request_approval(task).decision = "APPROVED"
        repo.commit()
        return db, tool, task.id


def _verifier(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VerifierHarness:
    return VerifierHarness(
        db.engine.url.render_as_string(hide_password=False), tmp_path / "w", monkeypatch
    )


def test_state_machine_graph_rules() -> None:
    # legal edge
    check_task_transition(TaskState.RECEIVED, TaskState.QUEUED, cancellation_requested=False)
    # terminal states are absorbing
    with pytest.raises(InvalidTransition, match="forbidden"):
        check_task_transition(TaskState.DONE, TaskState.QUEUED, cancellation_requested=False)
    # sticky cancel: only CANCELLED is reachable once cancellation is requested
    with pytest.raises(InvalidTransition, match="sticky"):
        check_task_transition(
            TaskState.PLANNING, TaskState.TOOL_EXECUTING, cancellation_requested=True
        )
    check_task_transition(TaskState.PLANNING, TaskState.CANCELLED, cancellation_requested=True)


def test_kill9_worker_midstep_recovers_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, tool, task_id = _make(tmp_path)
    # Drive the flow to TOOL_EXECUTING with the step RUNNING under a worker that is
    # then "killed" — its queue-job and task leases are left held but expired.
    with db.session_factory() as session:
        repo = TaskRepository(session)
        queue = DurableQueue(session)
        task = repo.get(task_id)
        queue.enqueue(task)
        job = queue.claim("dead-worker", 30)
        assert job is not None and job.status == "RUNNING"
        task = repo.get(task_id)
        repo.transition(task, TaskState.PLANNING, "planning", "dead-worker")
        repo.transition_step(task, task.steps[0], StepState.RUNNING, "dispatch", "dead-worker")
        repo.transition(task, TaskState.TOOL_EXECUTING, "dispatched", "dead-worker")
        repo.acquire_lease(task, "dead-worker", 30)
        repo.commit()
        past = utcnow() - timedelta(seconds=120)
        session.execute(update(QueueJob).where(QueueJob.task_id == task_id).values(lease_expires_at=past))
        session.execute(update(TaskFlow).where(TaskFlow.id == task_id).values(lease_expires_at=past))
        session.commit()

    with db.session_factory() as session:
        report = RecoveryWorker(session).recover()
    assert report.requeued_jobs == 1
    assert report.released_task_leases == 1

    # A fresh worker can re-claim the requeued job and resume the flow to DONE.
    with db.session_factory() as session:
        reclaimed = DurableQueue(session).claim("live-worker", 30)
        assert reclaimed is not None and reclaimed.status == "RUNNING"
    verifier = _verifier(db, tmp_path, monkeypatch)
    with db.session_factory() as session:
        Orchestrator(session, tool, verifier).run(
            TaskRepository(session).get(task_id), worker_id="live-worker"
        )
    with db.session_factory() as session:
        assert TaskRepository(session).get(task_id).status == "DONE"
    assert tool.calls == 1


def test_cancel_survives_restart_and_blocks_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, tool, task_id = _make(tmp_path)
    with db.session_factory() as session:
        repo = TaskRepository(session)
        queue = DurableQueue(session)
        task = repo.get(task_id)
        queue.enqueue(task)
        assert queue.claim("w", 30) is not None
        repo.cancel(repo.get(task_id))

    # "Restart": a recovery sweep plus a fresh worker attempt must not run the step.
    with db.session_factory() as session:
        RecoveryWorker(session).recover()
    verifier = _verifier(db, tmp_path, monkeypatch)
    with db.session_factory() as session:
        Orchestrator(session, tool, verifier).run(TaskRepository(session).get(task_id))

    with db.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task.status == "CANCELLED"
        assert task.cancellation_requested is True
    assert tool.calls == 0

    # Sticky cancel is enforced by the state machine even against a direct attempt.
    with db.session_factory() as session:
        repo = TaskRepository(session)
        with pytest.raises(InvalidTransition):
            repo.transition(repo.get(task_id), TaskState.PLANNING, "resume", "attacker")


def test_invalid_transition_raises_and_records_rejection(tmp_path: Path) -> None:
    db, _, task_id = _make(tmp_path)
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task = repo.get(task_id)  # RECEIVED
        with pytest.raises(InvalidTransition):
            repo.transition(task, TaskState.VERIFYING, "illegal jump", "attacker")
        # The accepted change never happened; commit persists only the audit row.
        session.commit()

    with db.session_factory() as session:
        rows = (
            session.scalars(
                select(StateTransition).where(
                    StateTransition.task_id == task_id, StateTransition.actor == "attacker"
                )
            )
            .all()
        )
        assert len(rows) == 1
        rejection = rows[0]
        assert rejection.reason.startswith("REJECTED")
        assert rejection.from_state == "RECEIVED"
        assert rejection.to_state == "VERIFYING"


def test_outbox_delivers_exactly_once_when_sender_crashes_before_ack(tmp_path: Path) -> None:
    db, _, _ = _make(tmp_path)
    adapter = FakeAdapter()
    sender = DeliveryWorker(db.session_factory, adapter, "sender")
    assert sender.dispatch_one() is True
    assert len(adapter.events) >= 1

    # Simulate a crash AFTER the external send but BEFORE the DELIVERED commit:
    # the row is left SENDING with an expired lease so a fresh sender re-claims it.
    with db.session_factory() as session:
        row = session.scalars(
            select(DeliveryOutbox).where(DeliveryOutbox.status == "SIMULATED")
        ).first()
        assert row is not None
        key = row.idempotency_key
        session.execute(
            update(DeliveryOutbox)
            .where(DeliveryOutbox.id == row.id)
            .values(
                status="SENDING",
                lease_owner="crashed",
                lease_expires_at=utcnow() - timedelta(seconds=120),
            )
        )
        session.commit()

    adapter2 = FakeAdapter()
    DeliveryWorker(db.session_factory, adapter2, "sender2").dispatch_one()
    # Downstream idempotency key still appears exactly once across both adapter instances.
    assert [k for _, k in adapter.events].count(key) == 1
    assert [k for _, k in adapter2.events].count(key) == 0
