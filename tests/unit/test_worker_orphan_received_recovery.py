"""R1-QUEUE-01 (T0021 R1-B10) — a TaskFlow accepted into RECEIVED but never
enqueued (submission process died between repository.create() and
DurableQueue.enqueue(); or orchestrator.spawn_child_flow which never enqueues)
must be drained by an idempotent worker-startup recovery sweep, exactly once,
with no duplicate execution.

Regression target: worker/__init__._recover_stale_flows scans only
TOOL_EXECUTING/RUNNING; DurableQueue.claim sees only QueueJob rows — so an
orphan RECEIVED flow hangs forever with no terminal or blocking state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from antigona.database import Database
from antigona.models import FlowStep, QueueJob, StepState, TaskFlow, TaskState
from antigona.queue import DurableQueue, build_broker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'q.db'}")
    database.create_all()
    return database


def _make_flow(
    db: Database, flow_id: str, status: str, *, with_job: bool = False
) -> None:
    with db.session_factory() as s:
        t = TaskFlow(
            id=flow_id, owner_id="o", goal="g", target_path="a.txt",
            content="x", idempotency_key=f"idem-{flow_id}", status=status,
        )
        t.steps.append(
            FlowStep(task_id=flow_id, index=0, title="s1",
                     input={"path": "a.txt"}, status=StepState.PENDING.value)
        )
        s.add(t)
        s.commit()
        if with_job:
            s.add(QueueJob(task_id=flow_id, lane="main", status="QUEUED",
                           correlation_id=f"corr-{flow_id}"))
            s.commit()


def _job_count(db: Database, flow_id: str) -> int:
    with db.session_factory() as s:
        return int(
            s.scalar(select(func.count()).select_from(QueueJob).where(
                QueueJob.task_id == flow_id)) or 0
        )


def test_recover_orphan_received_enqueues_and_makes_claimable(db: Database) -> None:
    from antigona.worker import _recover_orphan_received_flows

    _make_flow(db, "orphan-1", TaskState.RECEIVED.value)

    n = _recover_orphan_received_flows(db, build_broker(None), None, grace_seconds=0)
    assert n == 1

    with db.session_factory() as s:
        assert s.get(TaskFlow, "orphan-1").status == TaskState.QUEUED.value
        claimed = DurableQueue(s, build_broker(None)).claim("w1", 30)
        assert claimed is not None and claimed.task_id == "orphan-1"


def test_recover_orphan_received_is_idempotent_no_duplicate_job(db: Database) -> None:
    from antigona.worker import _recover_orphan_received_flows

    _make_flow(db, "orphan-2", TaskState.RECEIVED.value)

    first = _recover_orphan_received_flows(db, build_broker(None), None, grace_seconds=0)
    second = _recover_orphan_received_flows(db, build_broker(None), None, grace_seconds=0)
    assert (first, second) == (1, 0)
    assert _job_count(db, "orphan-2") == 1

    # single execution: claim once -> RUNNING, claim again -> nothing
    with db.session_factory() as s:
        q = DurableQueue(s, build_broker(None))
        assert q.claim("w1", 30) is not None
        assert q.claim("w2", 30) is None


def test_recover_orphan_skips_flows_within_grace(db: Database) -> None:
    from antigona.worker import _recover_orphan_received_flows

    _make_flow(db, "fresh-1", TaskState.RECEIVED.value)
    # default grace must not drain a flow a live submitter may still be enqueuing
    n = _recover_orphan_received_flows(db, build_broker(None), None)
    assert n == 0
    assert _job_count(db, "fresh-1") == 0
    with db.session_factory() as s:
        assert s.get(TaskFlow, "fresh-1").status == TaskState.RECEIVED.value


def test_recover_orphan_ignores_enqueued_and_terminal(db: Database) -> None:
    from antigona.worker import _recover_orphan_received_flows

    _make_flow(db, "queued-ok", TaskState.RECEIVED.value, with_job=True)
    _make_flow(db, "done-1", TaskState.DONE.value)
    _make_flow(db, "cancelled-1", TaskState.CANCELLED.value)

    n = _recover_orphan_received_flows(db, build_broker(None), None, grace_seconds=0)
    assert n == 0
    assert _job_count(db, "queued-ok") == 1
    assert _job_count(db, "done-1") == 0
    assert _job_count(db, "cancelled-1") == 0


def test_spawn_child_flow_orphan_is_recovered(db: Database, tmp_path: Path) -> None:
    """The exact R1-B10 scenario: a child from orchestrator.spawn_child_flow has
    no QueueJob; the sweep drains it into the durable queue exactly once."""
    from antigona.orchestrator import Orchestrator, spawn_child_flow
    from antigona.worker import _recover_orphan_received_flows

    with db.session_factory() as s:
        parent = TaskFlow(
            id="parent-1", owner_id="o", goal="parent", target_path="p.txt",
            content="p", idempotency_key="idem-parent",
            status=TaskState.RUNNING.value, depth=0, max_depth=3,
        )
        s.add(parent)
        s.commit()

        orch = Orchestrator.__new__(Orchestrator)
        from antigona.repository import TaskRepository
        orch.repository = TaskRepository(s)
        child = spawn_child_flow(
            orch, parent, goal="child", target_path="c.txt", content="c"
        )
        child_id = child.id

    assert _job_count(db, child_id) == 0  # orphan by construction

    n = _recover_orphan_received_flows(db, build_broker(None), None, grace_seconds=0)
    assert n == 1
    assert _job_count(db, child_id) == 1
    with db.session_factory() as s:
        assert s.get(TaskFlow, child_id).status == TaskState.QUEUED.value
