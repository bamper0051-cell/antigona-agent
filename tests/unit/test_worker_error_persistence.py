from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path

import pytest
from sqlalchemy import select

import antigona.worker as worker_module
from antigona.database import Database
from antigona.filesystem import SandboxUnavailable
from antigona.models import DeliveryOutbox, QueueJob, StateTransition
from antigona.queue import DurableQueue
from antigona.replay import ReplayEngine
from antigona.repository import CreateTask, LeaseConflict, TaskRepository


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (LeaseConflict("RAW_LEASE_EXCEPTION_MARKER"), "worker.lease_error"),
        (
            urllib.error.URLError("RAW_VERIFIER_EXCEPTION_MARKER"),
            "worker.verifier_error",
        ),
        (SandboxUnavailable("RAW_TOOL_EXCEPTION_MARKER"), "worker.tool_error"),
        (TimeoutError("RAW_TIMEOUT_EXCEPTION_MARKER"), "worker.timeout"),
        (RuntimeError("RAW_GENERIC_EXCEPTION_MARKER"), "worker.execution_error"),
    ],
    ids=("lease", "verifier", "tool", "timeout", "generic"),
)
def test_worker_failure_persists_and_logs_only_fixed_error_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    exception: Exception,
    expected_code: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'worker-errors.sqlite'}")
    database.create_all()
    marker = str(exception.reason if isinstance(exception, urllib.error.URLError) else exception)
    caplog.set_level(logging.INFO, logger="antigona")

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal="write a safe report",
                path="reports/out.txt",
                content="safe payload",
                idempotency_key="worker-error",
            ),
            correlation_id="worker-error-correlation",
        )
        queue = DurableQueue(session)
        queue.enqueue(task, correlation_id="worker-error-correlation")
        job = queue.claim("worker-1", 30)
        assert job is not None

        handler = worker_module._handle_worker_failure
        actual_code = handler(
            queue=queue,
            job=job,
            task=task,
            max_retries=0,
            exc=exception,
        )
        assert actual_code == expected_code

    with database.session_factory() as session:
        stored_job = session.scalar(select(QueueJob))
        assert stored_job is not None
        assert stored_job.status == "FAILED"
        assert stored_job.last_error == expected_code

        transitions = session.scalars(select(StateTransition)).all()
        outbox = session.scalars(select(DeliveryOutbox)).all()
        trajectory = ReplayEngine(session).get_trajectory(stored_job.task_id).to_dict()
        persisted = json.dumps(
            {
                "job": {"status": stored_job.status, "last_error": stored_job.last_error},
                "transitions": [row.reason for row in transitions],
                "outbox": [
                    {"payload": row.payload, "last_error": row.last_error} for row in outbox
                ],
                "trajectory": trajectory,
            },
            sort_keys=True,
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in persisted
    assert marker not in logged
    assert expected_code in logged
    assert "task_error" in logged


def test_worker_retry_also_uses_fixed_error_code(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'worker-retry.sqlite'}")
    database.create_all()
    marker = "RAW_RETRY_EXCEPTION_MARKER"

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal="write a safe report",
                path="reports/out.txt",
                content="safe payload",
                idempotency_key="worker-retry",
            )
        )
        queue = DurableQueue(session)
        queue.enqueue(task)
        job = queue.claim("worker-1", 30)
        assert job is not None

        handler = worker_module._handle_worker_failure
        code = handler(
            queue=queue,
            job=job,
            task=task,
            max_retries=1,
            exc=RuntimeError(marker),
        )

        assert code == "worker.execution_error"
        assert job.status == "QUEUED"
        assert job.last_error == code
        assert marker not in (job.last_error or "")
