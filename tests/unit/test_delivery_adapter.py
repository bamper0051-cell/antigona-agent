from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from antigona.database import Database
from antigona.delivery import DeliveryWorker, ProgressEvent, TelegramAdapter
from antigona.delivery.errors import sanitize_delivery_error
from antigona.models import DeliveryOutbox
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, TaskRepository


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[ProgressEvent] = []

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        self.calls.append(event)
        return True  # stands in for a working external adapter


def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'del.db'}")
    database.create_all()
    return database


def make_task(database: Database) -> str:
    with database.session_factory() as session:
        task, _ = TaskRepository(session).create(CreateTask("owner", "goal", "x.txt", "v", "k1"))
        DurableQueue(session).enqueue(task)
        return task.id


def outbox(task_id: str, key: str) -> DeliveryOutbox:
    return DeliveryOutbox(
        task_id=task_id,
        adapter="progress",
        event_type="transition",
        idempotency_key=key,
        payload={
            "task_id": task_id,
            "session_id": "s",
            "correlation_id": "c",
            "step_id": None,
            "status": "QUEUED",
            "message": "m",
        },
    )


def test_telegram_adapter_builds_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_open(req: Any, timeout: int = 0, **_kw: Any) -> Any:
        del timeout
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        seen["headers"] = dict(req.headers)

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

            def close(self) -> None:
                return None

        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    adapter = TelegramAdapter("TOPSECRET", "chat-1")
    adapter.deliver(ProgressEvent("tid", "s", "c", None, "QUEUED", "enqueued"), "k1")
    assert adapter.bot_token == "TOPSECRET"
    assert "TOPSECRET" in seen["url"]
    assert "tid" in seen["body"] and "enqueued" in seen["body"]
    # urllib normalizes header names to title-case-after-hyphen (Idempotency-Key -> Idempotency-key),
    # so match case-insensitively on the value the adapter actually sent.
    headers_lc = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers_lc["idempotency-key"] == "k1"


def test_delivery_claim_race_returns_none(tmp_path: Path) -> None:
    database = db(tmp_path)
    task_id = make_task(database)
    with database.session_factory() as session:
        session.add(outbox(task_id, "k1"))
        session.commit()
    # Isolate: task creation also emits progress outbox rows on state transitions.
    # The race test targets only the row we enqueued, so drop the others.
    with database.session_factory() as session:
        from sqlalchemy import delete

        session.execute(delete(DeliveryOutbox).where(DeliveryOutbox.idempotency_key != "k1"))
        session.commit()
    worker = DeliveryWorker(database.session_factory(), FakeAdapter())
    first = worker.claim()
    assert first is not None and first.idempotency_key == "k1"
    # Second claim must fail the CAS (rowcount != 1) and roll back, not raise.
    assert worker.claim() is None


def test_delivery_dispatch_delivers_and_marks_delivered(tmp_path: Path) -> None:
    database = db(tmp_path)
    task_id = make_task(database)
    with database.session_factory() as session:
        session.add(outbox(task_id, "k1"))
        session.commit()
    with database.session_factory() as session:
        from sqlalchemy import delete

        session.execute(delete(DeliveryOutbox).where(DeliveryOutbox.idempotency_key != "k1"))
        session.commit()
    adapter = FakeAdapter()
    worker = DeliveryWorker(database.session_factory(), adapter)
    assert worker.dispatch_one()
    assert len(adapter.calls) == 1
    with database.session_factory() as session:
        oid = session.scalar(
            select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == "k1")
        )
        item = session.get(DeliveryOutbox, oid)
        assert item is not None and item.status == "DELIVERED"


def test_delivery_no_pending_returns_false(tmp_path: Path) -> None:
    database = db(tmp_path)
    worker = DeliveryWorker(database.session_factory(), FakeAdapter())
    assert not worker.dispatch_one()


def test_claim_never_reclaims_failed_row(tmp_path: Path) -> None:
    """A row marked FAILED (terminal) must never be re-claimed, even with a past available_at."""
    database = db(tmp_path)
    task_id = make_task(database)
    with database.session_factory() as session:
        from sqlalchemy import delete

        session.add(outbox(task_id, "k-failed"))
        session.commit()
        session.execute(delete(DeliveryOutbox).where(DeliveryOutbox.idempotency_key != "k-failed"))
        session.execute(
            DeliveryOutbox.__table__.update()
            .where(DeliveryOutbox.idempotency_key == "k-failed")
            .values(status="FAILED")
        )
        session.commit()
    worker = DeliveryWorker(database.session_factory(), FakeAdapter())
    assert worker.claim() is None


def test_sanitize_delivery_error_never_leaks_endpoint_or_token() -> None:
    """Raw transport exception text (URLs, bot tokens) must never survive sanitization."""
    secret_bearing = RuntimeError(
        "Connection to https://api.telegram.org/bot123456789:AAFakeTokenValueXYZ/sendMessage failed"
    )
    code = sanitize_delivery_error(secret_bearing)
    assert "api.telegram.org" not in code
    assert "123456789:AAFakeTokenValueXYZ" not in code
    assert code == "delivery.execution_error"
