from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from antigona.database import Database
from antigona.delivery import DeliveryWorker, FakeAdapter
from antigona.lifecycle import ExecutionGuard
from antigona.models import DeliveryOutbox, QueueJob, TaskFlow, utcnow
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, TaskRepository
from antigona.security import read_private_credential


class Cancellable:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def prepared(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(f"sqlite:///{tmp_path/'lifecycle.db'}")
    database.create_all()
    with database.session_factory() as session:
        task, _ = TaskRepository(session).create(CreateTask("owner", "goal", "proof", "ok", "key"))
        job = DurableQueue(session).enqueue(task)
        return database, task.id, job.id


def test_periodic_heartbeat_prevents_adversarial_reclaim(tmp_path: Path) -> None:
    database, task_id, job_id = prepared(tmp_path)
    with database.session_factory() as session:
        job = DurableQueue(session).claim("worker-a", 1)
        assert job and job.id == job_id
        task = TaskRepository(session).get(task_id)
        TaskRepository(session).acquire_lease(task, "worker-a", 1)
    guard = ExecutionGuard(database.session_factory, job_id, task_id, "worker-a", 1, None)
    guard.start()
    time.sleep(1.4)
    try:
        with database.session_factory() as session:
            assert DurableQueue(session).claim("worker-b", 1) is None
            current = session.get(TaskFlow, task_id)
            queued = session.get(QueueJob, job_id)
            assert current and current.lease_owner == "worker-a"
            assert queued and queued.lease_owner == "worker-a" and queued.attempts == 1
    finally:
        guard.stop()
    assert not guard.thread.is_alive()


def test_durable_cancel_watcher_invokes_tool_cancel(tmp_path: Path) -> None:
    database, task_id, job_id = prepared(tmp_path)
    tool = Cancellable()
    with database.session_factory() as session:
        job = DurableQueue(session).claim("worker", 1)
        assert job
        task = TaskRepository(session).get(task_id)
        TaskRepository(session).acquire_lease(task, "worker", 1)
    guard = ExecutionGuard(database.session_factory, job_id, task_id, "worker", 1, tool)  # type: ignore[arg-type]
    guard.start()
    with database.session_factory() as session:
        TaskRepository(session).cancel(TaskRepository(session).get(task_id))
    for _ in range(30):
        if tool.cancelled:
            break
        time.sleep(0.1)
    guard.stop()
    assert tool.cancelled and not guard.thread.is_alive()


def test_atomic_outbox_claim_retry_and_idempotent_delivery(tmp_path: Path) -> None:
    database, task_id, _ = prepared(tmp_path)
    with database.session_factory() as session:
        events = list(session.scalars(select(DeliveryOutbox).where(DeliveryOutbox.task_id == task_id)))
        assert events
        adapter = FakeAdapter(fail_times=1)
        worker = DeliveryWorker(session, adapter)
        assert not worker.dispatch_one()
        pending = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.status == "PENDING"))
        assert pending and pending.attempts == 1 and pending.last_error
        pending.available_at = utcnow()
        session.commit()
        assert worker.dispatch_one()
        delivered = session.get(DeliveryOutbox, pending.id)
        assert delivered and delivered.status == "SIMULATED" and delivered.attempts == 2
        while worker.dispatch_one():
            pass
        assert len(adapter.events) == len(events)
        assert len({key for _, key in adapter.events}) == len(events)


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX uid semantics (geteuid/getuid/st_uid) unavailable on Windows (Wave 4)')
def test_private_credential_policy(tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text("secret\n")
    path.chmod(0o600)
    assert read_private_credential(str(path)) == "secret"
    path.chmod(0o640)
    with pytest.raises(PermissionError, match="0600"):
        read_private_credential(str(path))
    assert path.stat().st_uid == os.geteuid()
