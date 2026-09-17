"""P1-05 (Antigona R2 Codex review): Queue orphan grace window starvation.

Canonical finding — evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md:

    [P1] src/antigona/worker/__init__.py:211-265,419-420 — Startup sweep flows
    в RECEIVED с grace_cutoff_seconds=30 пропускает flow, созданные менее 30с
    назад, но sweep выполняется ровно один раз при старте процесса: если
    submitter падает сразу после create(), а worker стартует через 1–29с, flow
    никогда не перейдёт в QUEUED и навсегда останется в RECEIVED — тесты
    создают flow со старым timestamp — blocking
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from antigona.database import Database
from antigona.models import FlowStep, QueueJob, StepState, TaskFlow, TaskState
from antigona.queue import DurableQueue, build_broker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'q_p1_05.db'}")
    database.create_all()
    return database


def _create_orphan_flow(db: Database, flow_id: str, created_at: datetime) -> None:
    with db.session_factory() as s:
        t = TaskFlow(
            id=flow_id,
            owner_id="owner-1",
            goal="process data",
            target_path="out.txt",
            content="",
            idempotency_key=f"idem-{flow_id}",
            status=TaskState.RECEIVED.value,
            created_at=created_at,
        )
        t.steps.append(
            FlowStep(
                task_id=flow_id,
                index=0,
                title="step-1",
                input={"path": "out.txt"},
                status=StepState.PENDING.value,
            )
        )
        s.add(t)
        s.commit()


def test_orphan_created_within_startup_grace_is_recovered_by_loop_sweep(
    db: Database,
) -> None:
    """If worker starts 5s after task creation (within 30s grace), the task is skipped

    at startup, but MUST be picked up and enqueued once grace expires during normal
    worker operation without process restart.
    """
    from antigona.worker import _recover_orphan_received_flows, _run_worker_maintenance_sweep

    t0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    _create_orphan_flow(db, "orphan-starved", created_at=t0)

    broker = build_broker(None)

    # 1. Worker starts at T0 + 5s (within 30s grace)
    t_start = t0 + timedelta(seconds=5)
    with patch("antigona.worker.utcnow", return_value=t_start):
        n_startup = _recover_orphan_received_flows(db, broker, None, grace_seconds=30)
        assert n_startup == 0, "Startup sweep correctly skips fresh flow within grace"

    # Verify flow is still unqueued at T0 + 5s
    with db.session_factory() as s:
        flow = s.get(TaskFlow, "orphan-starved")
        assert flow.status == TaskState.RECEIVED.value
        job = s.scalar(select(QueueJob).where(QueueJob.task_id == "orphan-starved"))
        assert job is None

    # 2. Worker continues running; time reaches T0 + 35s (grace expired)
    t_later = t0 + timedelta(seconds=35)
    with patch("antigona.worker.utcnow", return_value=t_later):
        # On baseline: no periodic maintenance in worker loop -> function does not exist or not called
        n_loop = _run_worker_maintenance_sweep(db, broker, None)
        assert n_loop == 1, "Loop maintenance sweep must recover the expired orphan"

    # 3. Flow is now QUEUED and claimable
    with db.session_factory() as s:
        flow = s.get(TaskFlow, "orphan-starved")
        assert flow.status == TaskState.QUEUED.value
        claimed = DurableQueue(s, broker).claim("w1", 30)
        assert claimed is not None
        assert claimed.task_id == "orphan-starved"


def test_post_startup_orphan_recovered_by_running_worker(db: Database) -> None:
    """An orphan created while the worker is already running is recovered once grace passes."""
    from antigona.worker import _run_worker_maintenance_sweep

    t0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    broker = build_broker(None)

    # Orphan created at T0 + 100s while worker is already running
    t_create = t0 + timedelta(seconds=100)
    _create_orphan_flow(db, "orphan-mid-run", created_at=t_create)

    # Worker check at T0 + 110s (grace not yet passed)
    with patch("antigona.worker.utcnow", return_value=t0 + timedelta(seconds=110)):
        n_early = _run_worker_maintenance_sweep(db, broker, None)
        assert n_early == 0

    # Worker check at T0 + 135s (grace passed)
    with patch("antigona.worker.utcnow", return_value=t0 + timedelta(seconds=135)):
        n_ready = _run_worker_maintenance_sweep(db, broker, None)
        assert n_ready == 1

    with db.session_factory() as s:
        flow = s.get(TaskFlow, "orphan-mid-run")
        assert flow.status == TaskState.QUEUED.value
