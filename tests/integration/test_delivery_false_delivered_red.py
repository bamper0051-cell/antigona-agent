"""R1-DELIVERY-01 RED — a simulated/mock dispatch must not persist as DELIVERED.

Invariant (R2 wave 3): `DeliveryOutbox.status == "DELIVERED"` must mean a real
external transmission occurred; a mock/simulated dispatch must persist a distinct
terminal status (`SIMULATED`) with no `delivered_at`, so it can never be mistaken
for production delivery.

Base behaviour (candidate + bb03a2a8..95c16ea6): with `delivery_mock=True`
(the default, `ANTIGONA_DELIVERY_MOCK` env default is `"1"`), `DeliveryWorker`
sets `status = "DELIVERED"` and `delivered_at = utcnow()` after any non-raising
adapter call — including the mock branch that contacts nobody.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.config import Settings
from antigona.database import Database
from antigona.delivery import DeliveryWorker, Router
from antigona.models import DeliveryOutbox, TaskFlow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'del_red.db'}")
    database.create_all()
    return database


def _seed(session: Session) -> TaskFlow:
    task = TaskFlow(goal="g", owner_id="owner-1", target_path="o.txt")
    session.add(task)
    session.commit()
    return task


def test_mock_dispatch_is_not_persisted_as_delivered(db: Database, tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'del_red.db'}",
        workspace=tmp_path,
        delivery_mock=True,
    )
    router = Router(settings)
    with db.session_factory() as session:
        task = _seed(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="discord",
            event_type="transition",
            idempotency_key=f"red-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "s",
                "correlation_id": "c",
                "step_id": None,
                "status": "DONE",
                "message": "mock only",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, router=router, worker_id="w-red")
        assert worker.dispatch_one() is True  # item reaches a terminal state

        row = session.get(DeliveryOutbox, item_id)
        assert row is not None
        assert row.status == "SIMULATED", (
            f"mock dispatch persisted as {row.status!r} — indistinguishable from a real send"
        )
        assert row.delivered_at is None


class _RealAdapter:
    """Adapter double that reports a genuine external transmission."""

    name = "real"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def deliver(self, event: object, idempotency_key: str) -> bool:
        self.calls.append(idempotency_key)
        return True


def test_real_transmission_is_persisted_as_delivered(db: Database, tmp_path: Path) -> None:
    adapter = _RealAdapter()
    with db.session_factory() as session:
        task = _seed(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="progress",
            event_type="transition",
            idempotency_key=f"real-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "s",
                "correlation_id": "c",
                "step_id": None,
                "status": "DONE",
                "message": "real send",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, adapter=adapter, worker_id="w-real")
        assert worker.dispatch_one() is True
        assert adapter.calls  # the external send actually ran

        row = session.get(DeliveryOutbox, item_id)
        assert row is not None
        assert row.status == "DELIVERED"
        assert row.delivered_at is not None


def test_send_failure_is_never_delivered(db: Database, tmp_path: Path) -> None:
    from antigona.delivery.adapter import FakeAdapter

    adapter = FakeAdapter(fail_times=1)
    with db.session_factory() as session:
        task = _seed(session)
        item = DeliveryOutbox(
            task_id=task.id,
            adapter="progress",
            event_type="transition",
            idempotency_key=f"fail-{uuid.uuid4().hex[:8]}",
            payload={
                "task_id": task.id,
                "session_id": "s",
                "correlation_id": "c",
                "step_id": None,
                "status": "DONE",
                "message": "boom",
            },
        )
        session.add(item)
        session.commit()
        item_id = item.id

        worker = DeliveryWorker(session, adapter=adapter, worker_id="w-fail")
        assert worker.dispatch_one() is False

        row = session.get(DeliveryOutbox, item_id)
        assert row is not None
        assert row.status not in ("DELIVERED", "SIMULATED")
        assert row.delivered_at is None
