from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.config import Settings
from antigona.database import Database
from antigona.delivery import DeliveryWorker, Router
from antigona.models import DeliveryOutbox, TaskFlow, utcnow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_delivery.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    return database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test_delivery.db",
        workspace=tmp_path,
        delivery_mock=True,
    )


def _seed_task(session: Session) -> TaskFlow:
    task = TaskFlow(
        goal="Test multi-channel delivery task",
        owner_id="owner-1",
        target_path="output.txt",
    )
    session.add(task)
    session.commit()
    return task


@pytest.mark.parametrize("channel", ["discord", "slack", "whatsapp", "signal", "email"])
def test_outbox_routes_by_channel(db: Database, settings: Settings, channel: str) -> None:
    router = Router(settings)
    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter=channel,
            event_type="transition",
            idempotency_key=f"idem-{channel}-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "DONE",
                "message": f"Delivered via {channel}",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="worker-test")
        delivered = worker.dispatch_one()

        assert delivered is True
        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        # delivery_mock=True: a simulated dispatch must be a distinct terminal
        # status, never DELIVERED, and must carry no delivered_at timestamp.
        assert refetched.status == "SIMULATED"
        assert refetched.delivered_at is None

        # Verify adapter recorded the event
        adapter = router.get_adapter(channel)
        assert hasattr(adapter, "delivered_events")
        events = adapter.delivered_events
        assert len(events) >= 1
        assert events[-1][0].message == f"Delivered via {channel}"


def test_failure_keeps_message_in_outbox(db: Database, settings: Settings) -> None:
    router = Router(settings)
    # Inject failure into discord adapter
    discord_adapter = router.get_adapter("discord")
    discord_adapter.fail_times = 1

    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="discord",
            event_type="transition",
            idempotency_key=f"idem-fail-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "RUNNING",
                "message": "Failing delivery test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="worker-fail-test")
        delivered = worker.dispatch_one()

        assert delivered is False
        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "PENDING"
        assert refetched.attempts == 1
        assert refetched.available_at > utcnow()
        assert refetched.last_error is not None
        # last_error is a sanitized fixed code, never the raw adapter exception text
        # (which could embed a webhook URL or token in real deployments).
        assert refetched.last_error == "delivery.execution_error"
        assert "discord delivery failure" not in refetched.last_error


def test_retry_delivers_after_recovery(db: Database, settings: Settings) -> None:
    router = Router(settings)
    discord_adapter = router.get_adapter("discord")
    discord_adapter.fail_times = 1

    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="discord",
            event_type="transition",
            idempotency_key=f"idem-retry-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "DONE",
                "message": "Retry recovery test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="worker-retry-test")
        # First attempt fails
        assert worker.dispatch_one() is False

        # Simulate time passing and adapter recovery
        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        refetched.available_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

        # Second attempt succeeds
        assert worker.dispatch_one() is True
        final_row = session.get(DeliveryOutbox, item_id)
        assert final_row is not None
        assert final_row.status == "SIMULATED"  # delivery_mock=True


def test_progress_backward_compat(db: Database, settings: Settings) -> None:
    router = Router(settings)
    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="progress",
            event_type="transition",
            idempotency_key=f"idem-progress-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "DONE",
                "message": "Backward compat test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="worker-compat")
        assert worker.dispatch_one() is True

        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "SIMULATED"  # delivery_mock=True


def test_worker_fallback_settings_router(db: Database, settings: Settings) -> None:
    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="discord",
            event_type="transition",
            idempotency_key=f"idem-fallback-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "DONE",
                "message": "Worker fallback router test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        # Worker created without adapter or router, but with settings
        worker = DeliveryWorker(session, settings=settings, worker_id="worker-fallback")
        assert worker.dispatch_one() is True

        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "SIMULATED"  # delivery_mock=True


def test_unknown_channel_becomes_failed_immediately_without_retry(
    db: Database, settings: Settings
) -> None:
    """A permanent (config/unknown-channel) error must never be retried."""
    router = Router(settings)
    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="not-a-real-channel",
            event_type="transition",
            idempotency_key=f"idem-unknown-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "DONE",
                "message": "Unknown channel test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="worker-unknown")
        assert worker.dispatch_one() is False

        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "FAILED"
        assert refetched.attempts == 1
        assert refetched.last_error == "delivery.unknown_channel"

        # claim() must never resurrect a FAILED row, even though available_at
        # was never pushed into the future for a terminal failure.
        assert worker.claim() is None


def test_transient_retry_exhaustion_becomes_failed(db: Database, tmp_path: Path) -> None:
    """After delivery_max_attempts consecutive transient failures, the row is terminal FAILED."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test_delivery.db",
        workspace=tmp_path,
        delivery_mock=True,
        delivery_max_attempts=2,
    )
    router = Router(settings)
    discord_adapter = router.get_adapter("discord")
    discord_adapter.fail_times = 10  # more than delivery_max_attempts

    with db.session_factory() as session:
        task = _seed_task(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="discord",
            event_type="transition",
            idempotency_key=f"idem-exhaust-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "sess-1",
                "correlation_id": "corr-1",
                "step_id": None,
                "status": "RUNNING",
                "message": "Exhaustion test",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(
            session, router=router, settings=settings, worker_id="worker-exhaust"
        )

        # Attempt 1: transient failure, still retryable.
        assert worker.dispatch_one() is False
        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "PENDING"
        assert refetched.attempts == 1

        refetched.available_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

        # Attempt 2 == delivery_max_attempts: exhausted, terminal FAILED.
        assert worker.dispatch_one() is False
        refetched = session.get(DeliveryOutbox, item_id)
        assert refetched is not None
        assert refetched.status == "FAILED"
        assert refetched.attempts == 2

        # FAILED is terminal: claim() must not pick it back up even though
        # available_at was left in the past by the last PENDING write.
        assert worker.claim() is None
