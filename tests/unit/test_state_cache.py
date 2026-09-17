"""Unit tests for the P4.3 state cache.

The cache is a read accelerator, never an authority. These tests pin the three
properties that make that true: write-after-commit only, database wins on a
miss, and a broken cache cannot break (or bypass) a transition.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.database import Database
from antigona.durable.state_cache import (
    DEFAULT_TTL_SECONDS,
    InMemoryStateBackend,
    RedisStateBackend,
    StateCache,
    build_state_cache,
    read_state,
)
from antigona.durable.state_machine import InvalidTransition
from antigona.models import TaskFlow, TaskState
from antigona.repository import TaskRepository


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BrokenBackend:
    """Every operation fails; the cache must absorb all of it."""

    def get(self, key: str) -> str | None:
        raise ConnectionError("cache down")

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        raise ConnectionError("cache down")

    def delete(self, key: str) -> None:
        raise ConnectionError("cache down")


def _database(tmp_path: Any) -> Database:
    database = Database(f"sqlite:///{tmp_path}/state_cache.db")
    database.create_all()
    return database


def _task(session: Any, key: str = "cache-1") -> TaskFlow:
    task = TaskFlow(owner_id="owner-1", goal="cache", target_path=".", idempotency_key=key)
    session.add(task)
    session.commit()
    return task


def test_cache_written_after_commit_only(tmp_path: Any) -> None:
    cache = StateCache(InMemoryStateBackend())
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        repository = TaskRepository(session, cache)
        repository.transition(task, TaskState.QUEUED, "enqueue", "gateway")

        assert cache.get(task.id) is None, "an uncommitted transition must not be cached"
        session.commit()
        assert cache.get(task.id) == TaskState.QUEUED.value


def test_rollback_invalidates_instead_of_publishing(tmp_path: Any) -> None:
    backend = InMemoryStateBackend()
    cache = StateCache(backend)
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        cache.put(task.id, TaskState.RECEIVED.value)

        TaskRepository(session, cache).transition(task, TaskState.QUEUED, "enqueue", "gateway")
        session.rollback()

        assert cache.get(task.id) is None, "a rolled-back transition must invalidate, not publish"
        # The database is untouched, and it is the arbiter.
        with database.session_factory() as fresh:
            assert read_state(fresh, task.id, cache) == TaskState.RECEIVED.value


def test_cache_miss_falls_back_to_db(tmp_path: Any) -> None:
    cache = StateCache(InMemoryStateBackend())
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        assert cache.get(task.id) is None

        assert read_state(session, task.id, cache) == TaskState.RECEIVED.value
        # The miss refilled the cache from the database.
        assert cache.get(task.id) == TaskState.RECEIVED.value
        # A disabled cache still answers from SQL.
        assert read_state(session, task.id, StateCache(backend=None)) == TaskState.RECEIVED.value
        assert read_state(session, "does-not-exist", cache) is None


def test_db_wins_when_cache_is_stale(tmp_path: Any) -> None:
    """A poisoned cache entry is corrected by the next committed transition."""
    cache = StateCache(InMemoryStateBackend())
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        cache.put(task.id, "TOTALLY-WRONG")

        TaskRepository(session, cache).transition(task, TaskState.QUEUED, "enqueue", "gateway")
        session.commit()

        assert cache.get(task.id) == TaskState.QUEUED.value
        assert read_state(session, task.id, cache) == task.status


def test_cache_error_does_not_break_transition(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    cache = StateCache(BrokenBackend())
    database = _database(tmp_path)

    with caplog.at_level("INFO", logger="antigona"):
        with database.session_factory() as session:
            task = _task(session)
            TaskRepository(session, cache).transition(task, TaskState.QUEUED, "enqueue", "gateway")
            session.commit()

            assert task.status == TaskState.QUEUED.value
            assert read_state(session, task.id, cache) == TaskState.QUEUED.value

    assert "state_cache.error" in caplog.text


def test_ttl_and_invalidation_on_transition(tmp_path: Any) -> None:
    clock = Clock()
    cache = StateCache(InMemoryStateBackend(clock=clock), ttl_seconds=60)
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        TaskRepository(session, cache).transition(task, TaskState.QUEUED, "enqueue", "gateway")
        session.commit()
        assert cache.get(task.id) == TaskState.QUEUED.value

        clock.advance(59)
        assert cache.get(task.id) == TaskState.QUEUED.value
        clock.advance(2)
        assert cache.get(task.id) is None, "TTL bounds how long a stale entry can survive"

        # Every transition republishes, so the cache tracks the journal.
        TaskRepository(session, cache).transition(task, TaskState.PLANNING, "plan", "worker")
        session.commit()
        assert cache.get(task.id) == TaskState.PLANNING.value

        cache.invalidate(task.id)
        assert cache.get(task.id) is None


def test_disabled_cache_is_a_no_op(tmp_path: Any) -> None:
    cache = build_state_cache(None)
    assert cache.enabled is False
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        TaskRepository(session, cache).transition(task, TaskState.QUEUED, "enqueue", "gateway")
        session.commit()
        assert cache.get(task.id) is None
        cache.put(task.id, "X")
        cache.invalidate(task.id)
        assert cache.get(task.id) is None
        assert read_state(session, task.id, cache) == TaskState.QUEUED.value


def test_build_state_cache_uses_redis_backend_lazily() -> None:
    cache = build_state_cache("redis://127.0.0.1:6379/0", ttl_seconds=30)
    assert cache.enabled is True
    assert cache.ttl_seconds == 30
    backend = cache.backend
    assert isinstance(backend, RedisStateBackend)
    assert backend._client is None, "the redis client must stay unbuilt until first use"
    assert StateCache(InMemoryStateBackend()).ttl_seconds == DEFAULT_TTL_SECONDS


def test_redis_backend_degrades_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    class FailingClient:
        def get(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise ConnectionError("redis down")

        def delete(self, key: str) -> None:
            raise ConnectionError("redis down")

    backend = RedisStateBackend("redis://ignored/0", client=FailingClient())
    with caplog.at_level("INFO", logger="antigona"):
        backend.set("state:x", "QUEUED", 30)
        assert backend.degraded is True
        assert backend.get("state:x") is None
        backend.delete("state:x")

    assert "state_cache.degraded" in caplog.text


def test_redis_backend_roundtrip_with_fake_client() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.ttls: dict[str, int | None] = {}

        def get(self, key: str) -> str | None:
            return self.values.get(key)

        def set(self, key: str, value: str, ex: int | None = None) -> None:
            self.values[key] = value
            self.ttls[key] = ex

        def delete(self, key: str) -> None:
            self.values.pop(key, None)

    client = FakeClient()
    cache = StateCache(RedisStateBackend("redis://ignored/0", client=client), ttl_seconds=42)
    cache.put("task-1", "QUEUED")

    assert client.values["state:task-1"] == "QUEUED"
    assert client.ttls["state:task-1"] == 42
    assert cache.get("task-1") == "QUEUED"
    cache.invalidate("task-1")
    assert cache.get("task-1") is None


def test_cache_never_opens_a_path_to_done(tmp_path: Any) -> None:
    """DONE stays Verifier-only: the cache sits downstream of the graph check."""
    cache = StateCache(InMemoryStateBackend())
    database = _database(tmp_path)

    with database.session_factory() as session:
        task = _task(session)
        repository = TaskRepository(session, cache)
        for target, reason in (
            (TaskState.QUEUED, "enqueue"),
            (TaskState.PLANNING, "plan"),
            (TaskState.TOOL_EXECUTING, "execute"),
            (TaskState.OBSERVING, "observe"),
            (TaskState.VERIFYING, "verify"),
        ):
            repository.transition(task, target, reason, "worker")
        session.commit()
        assert cache.get(task.id) == TaskState.VERIFYING.value

        with pytest.raises(InvalidTransition):
            repository.transition(task, TaskState.DONE, "cheat", "worker")
        session.rollback()

        # Neither the database nor the cache learned a forbidden state.
        with database.session_factory() as fresh:
            refreshed = fresh.get(TaskFlow, task.id)
            assert refreshed is not None and refreshed.status == TaskState.VERIFYING.value
        assert cache.get(task.id) != TaskState.DONE.value
