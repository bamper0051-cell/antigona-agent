"""Unit tests for the P4.3 task brokers.

Nothing here touches a real Redis: ``InMemoryBroker`` is the CI implementation
and ``RedisTaskBroker`` is driven against an injected fake client. A socket
monkeypatch proves the lazy import never opens a connection on its own.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from antigona.database import Database
from antigona.models import TaskFlow
from antigona.queue import (
    DurableQueue,
    InMemoryBroker,
    NullBroker,
    RedisTaskBroker,
    build_broker,
)
from antigona.queue.redis_broker import INFLIGHT_KEY, QUEUE_KEY


class FakeRedis:
    """Async-shaped stand-in for ``redis.asyncio.Redis`` (no sockets involved)."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail_on = fail_on or set()
        self.closed = False

    def _guard(self, operation: str) -> None:
        if operation in self.fail_on:
            raise ConnectionError(f"redis unavailable during {operation}")

    async def rpush(self, key: str, value: str) -> None:
        self._guard("rpush")
        self.lists.setdefault(key, []).append(value)

    async def lpush(self, key: str, value: str) -> None:
        self._guard("lpush")
        self.lists.setdefault(key, []).insert(0, value)

    async def blpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        self._guard("blpop")
        for key in keys:
            values = self.lists.get(key)
            if values:
                return (key, values.pop(0))
        return None

    async def hset(self, key: str, field: str, value: str) -> None:
        self._guard("hset")
        self.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key: str, field: str) -> None:
        self._guard("hdel")
        self.hashes.setdefault(key, {}).pop(field, None)

    async def aclose(self) -> None:
        self.closed = True


def _database(tmp_path: Any) -> Database:
    database = Database(f"sqlite:///{tmp_path}/broker.db")
    database.create_all()
    return database


def test_inmemory_broker_enqueue_dequeue_ack() -> None:
    broker = InMemoryBroker()

    async def scenario() -> None:
        await broker.enqueue("task-1")
        await broker.enqueue("task-2")
        assert broker.pending() == ["task-1", "task-2"]

        first = await broker.dequeue(timeout=0.01)
        assert first == "task-1"
        assert broker.inflight() == {"task-1"}

        await broker.ack("task-1")
        assert broker.inflight() == set()

        assert await broker.dequeue(timeout=0.01) == "task-2"
        await broker.ack("task-2")
        assert await broker.dequeue(timeout=0.01) is None
        await broker.close()

    asyncio.run(scenario())
    assert broker.closed is True


def test_nack_requeues() -> None:
    broker = InMemoryBroker()

    async def scenario() -> None:
        await broker.enqueue("task-1")
        assert await broker.dequeue(timeout=0.01) == "task-1"
        await broker.nack("task-1")
        assert broker.inflight() == set()
        # Re-queued at the head so the wake-up is not lost.
        assert broker.pending() == ["task-1"]
        assert await broker.dequeue(timeout=0.01) == "task-1"

    asyncio.run(scenario())


def test_null_broker_when_redis_url_absent() -> None:
    assert isinstance(build_broker(None), NullBroker)
    assert isinstance(build_broker(""), NullBroker)
    assert isinstance(build_broker("redis://127.0.0.1:6379/0"), RedisTaskBroker)

    broker = NullBroker()

    async def scenario() -> None:
        await broker.enqueue("task-1")
        assert await broker.dequeue(timeout=0.01) is None
        await broker.ack("task-1")
        await broker.nack("task-1")
        await broker.close()

    asyncio.run(scenario())
    broker.signal("task-1")
    assert broker.wait(0.001) is None


def test_redis_broker_lazy_import_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the broker must not import redis nor open any socket."""
    opened: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        opened.append(args)
        raise AssertionError("no network access is allowed in unit tests")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    broker = RedisTaskBroker("redis://127.0.0.1:6379/0")

    assert broker.degraded is False
    assert broker._client is None, "the redis client must stay unbuilt until first use"
    assert opened == []


def test_redis_broker_roundtrip_with_fake_client() -> None:
    fake = FakeRedis()
    broker = RedisTaskBroker("redis://ignored/0", client=fake, visibility_timeout=5.0)

    async def scenario() -> None:
        await broker.enqueue("task-1")
        assert fake.lists[QUEUE_KEY.format(lane="main")] == ["task-1"]

        assert await broker.dequeue(timeout=0.01) == "task-1"
        assert "task-1" in fake.hashes[INFLIGHT_KEY.format(lane="main")]

        await broker.ack("task-1")
        assert fake.hashes[INFLIGHT_KEY.format(lane="main")] == {}
        assert await broker.dequeue(timeout=0.01) is None

        await broker.enqueue("task-2", lane="fast")
        assert await broker.dequeue(timeout=0.01, lane="fast") == "task-2"
        await broker.nack("task-2", lane="fast")
        assert fake.lists[QUEUE_KEY.format(lane="fast")] == ["task-2"]

        await broker.close()

    asyncio.run(scenario())
    assert fake.closed is True
    assert broker.degraded is False


def test_redis_down_degrades_to_sql_polling_with_warning(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead Redis costs latency only: SQL keeps accepting and serving work."""
    broker = RedisTaskBroker("redis://ignored/0", client=FakeRedis(fail_on={"rpush", "blpop"}))
    database = _database(tmp_path)

    with caplog.at_level("INFO", logger="antigona"):
        with database.session_factory() as session:
            task = TaskFlow(
                owner_id="owner-1",
                goal="degrade",
                target_path=".",
                idempotency_key="redis-down",
            )
            session.add(task)
            session.commit()
            job = DurableQueue(session, broker).enqueue(task)

            assert broker.degraded is True
            assert "queue.broker.degraded" in caplog.text
            # SQL remains the source of truth: the job is claimable regardless.
            claimed = DurableQueue(session, broker).claim("worker-1", 30)
            assert claimed is not None and claimed.id == job.id

    # Once degraded, the broker is a no-op that keeps the poll cadence.
    assert broker.wait(0.001) is None
    assert asyncio.run(broker.dequeue(timeout=0.001)) is None


def test_durable_queue_signals_broker_after_commit(tmp_path: Any) -> None:
    broker = InMemoryBroker()
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = TaskFlow(
            owner_id="owner-1",
            goal="signal",
            target_path=".",
            idempotency_key="signal-1",
        )
        session.add(task)
        session.commit()

        job = DurableQueue(session, broker).enqueue(task)
        assert broker.pending() == [task.id], "enqueue must wake a worker up"
        assert broker.wait(0.001) == task.id

        # A retry re-arms the wake-up too.
        DurableQueue(session, broker).retry(job, "boom", delay_seconds=0)
        assert broker.pending() == [task.id]


def test_durable_queue_without_broker_is_unchanged(tmp_path: Any) -> None:
    """redis_url=None path: DurableQueue behaves exactly as before P4.3."""
    database = _database(tmp_path)
    with database.session_factory() as session:
        task = TaskFlow(
            owner_id="owner-1",
            goal="no broker",
            target_path=".",
            idempotency_key="no-broker",
        )
        session.add(task)
        session.commit()

        queue = DurableQueue(session)
        assert queue.broker is None
        job = queue.enqueue(task)
        assert job.status == "QUEUED"
        assert queue.claim("worker-1", 30) is not None


def test_sync_bridge_degrades_inside_running_loop() -> None:
    """A sync signal from inside a live loop degrades instead of deadlocking."""
    broker = RedisTaskBroker("redis://ignored/0", client=FakeRedis())

    async def scenario() -> None:
        broker.signal("task-1")

    asyncio.run(scenario())
    assert broker.degraded is True


def test_sync_signal_and_wait_use_private_loop() -> None:
    fake = FakeRedis()
    broker = RedisTaskBroker("redis://ignored/0", client=fake)

    broker.signal("task-1")
    assert fake.lists[QUEUE_KEY.format(lane="main")] == ["task-1"]
    assert broker.wait(0.01) == "task-1"
    assert broker.wait(0.01) is None
    assert broker.degraded is False
