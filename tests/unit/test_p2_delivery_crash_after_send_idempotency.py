"""P2 (Antigona R2 Codex review): Delivery crash-after-send idempotency.

Canonical finding — evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md:

    [P2] src/antigona/delivery/worker.py:48-83,101-143;
    src/antigona/delivery/adapter.py:178-212;
    tests/chaos/test_durable_state_machine.py:199-227 — Реальный provider
    принимает сообщение, процесс падает до commit DELIVERED, после lease expiry
    новый процесс отправляет его повторно; локальные seen_keys утрачены, а
    внешний API не обязан поддерживать произвольный Idempotency-Key — возможны
    дубликаты Telegram/email/Slack-сообщений — chaos-тест переиспользует тот же
    mock FakeAdapter с сохранённым in-memory set и вообще не выполняет реальную
    передачу — blocking
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import update

from antigona.database import Database
from antigona.delivery import DeliveryWorker, ProgressEvent, TelegramAdapter
from antigona.models import DeliveryOutbox, TaskFlow, utcnow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'del_p2.db'}")
    database.create_all()
    return database


def _create_outbox_item(session, task_id: str, key: str, adapter: str = "progress") -> str:
    item = DeliveryOutbox(
        task_id=task_id,
        adapter=adapter,
        event_type="transition",
        idempotency_key=key,
        payload={
            "task_id": task_id,
            "session_id": "owner-1",
            "correlation_id": "corr-1",
            "step_id": None,
            "status": "RUNNING",
            "message": "executing task",
        },
    )
    session.add(item)
    session.commit()
    return item.id


class CountingExternalAdapter:
    """Simulates a real external delivery provider (e.g. Telegram / Slack / Email)."""

    name = "counting"

    def __init__(self) -> None:
        self.call_count = 0
        self.events: list[tuple[ProgressEvent, str]] = []

    def deliver(self, event: ProgressEvent, idempotency_key: str) -> bool:
        self.call_count += 1
        self.events.append((event, idempotency_key))
        return True


def test_crash_after_send_does_not_duplicate_external_send_on_restart(db: Database) -> None:
    """When a worker transmits an external message but crashes before DELIVERED commit,

    a fresh worker on process restart MUST NOT send a duplicate message to the external provider.
    """
    with db.session_factory() as session:
        task = TaskFlow(goal="goal", owner_id="owner-1", target_path="out.txt")
        session.add(task)
        session.commit()
        task_id = task.id
        item_id = _create_outbox_item(session, task_id, "idem-crash-1")

    # Worker 1 runs and transmits
    adapter1 = CountingExternalAdapter()
    worker1 = DeliveryWorker(db.session_factory, adapter1, "worker-1")
    assert worker1.dispatch_one() is True
    assert adapter1.call_count == 1

    # Simulate crash AFTER external transmission:
    # DB row is rolled back/reset to SENDING with expired lease as if worker 1 died before final commit
    with db.session_factory() as session:
        session.execute(
            update(DeliveryOutbox)
            .where(DeliveryOutbox.id == item_id)
            .values(
                status="SENDING",
                lease_owner="crashed-w1",
                lease_expires_at=utcnow() - timedelta(seconds=60),
            )
        )
        session.commit()

    # Worker 2 starts fresh (new process, fresh adapter instance with empty memory)
    adapter2 = CountingExternalAdapter()
    worker2 = DeliveryWorker(db.session_factory, adapter2, "worker-2")
    assert worker2.dispatch_one() is True

    # Invariant: External send was NOT duplicated!
    assert adapter2.call_count == 0, (
        f"Duplicate external message was sent! Worker 2 called deliver() {adapter2.call_count} times."
    )

    with db.session_factory() as session:
        row = session.get(DeliveryOutbox, item_id)
        assert row is not None
        assert row.status == "DELIVERED"
        assert row.delivered_at is not None


def test_telegram_real_send_idempotent_across_restarts(db: Database) -> None:
    """Telegram HTTP sendMessage is called exactly once despite crash before status commit."""
    with db.session_factory() as session:
        task = TaskFlow(goal="goal", owner_id="owner-1", target_path="out.txt")
        session.add(task)
        session.commit()
        item_id = _create_outbox_item(session, task.id, "idem-tg-crash", adapter="telegram")

    http_calls: list[str] = []

    def mock_urlopen(req: Any, timeout: int = 10, **_kwargs: Any) -> Any:
        del timeout
        http_calls.append(req.full_url)

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok": true, "result": {"message_id": 12345}}'
        return _Resp()

    with patch("urllib.request.urlopen", mock_urlopen):
        # Worker 1 transmits
        tg_adapter1 = TelegramAdapter("TOKEN", "CHAT_ID", mock=False)
        worker1 = DeliveryWorker(db.session_factory, tg_adapter1, "worker-1")
        assert worker1.dispatch_one() is True
        assert len(http_calls) == 1

        # Simulate crash before status commit
        with db.session_factory() as session:
            session.execute(
                update(DeliveryOutbox)
                .where(DeliveryOutbox.id == item_id)
                .values(
                    status="SENDING",
                    lease_owner="crashed",
                    lease_expires_at=utcnow() - timedelta(seconds=60),
                )
            )
            session.commit()

        # Worker 2 starts fresh with separate TelegramAdapter instance
        tg_adapter2 = TelegramAdapter("TOKEN", "CHAT_ID", mock=False)
        worker2 = DeliveryWorker(db.session_factory, tg_adapter2, "worker-2")
        assert worker2.dispatch_one() is True

        # Invariant: urllib.request.urlopen was NOT called a second time
        assert len(http_calls) == 1, f"Telegram API was called {len(http_calls)} times (duplicate message sent!)"
