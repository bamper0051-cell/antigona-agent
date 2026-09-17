"""P4.3 checkpoint: state machine + cache + broker + async storage together.

The scenario runs a task through the whole legal path, then re-reads everything
through the *async* layer to prove both layers see one database. Redis and
Postgres are mocked/absent by construction: the broker is ``InMemoryBroker`` and
the cache backend is in-process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from antigona.database import Database
from antigona.durable.state_cache import InMemoryStateBackend, StateCache, read_state
from antigona.durable.state_machine import VERIFIER_ONLY_TRANSITIONS, InvalidTransition
from antigona.models import TaskFlow, TaskState
from antigona.queue import DurableQueue, InMemoryBroker
from antigona.repository import CreateTask, TaskRepository
from antigona.storage import build_async_engine, get_session
from antigona.storage.models import StateTransition

WORKER_PATH: tuple[tuple[TaskState, str], ...] = (
    (TaskState.PLANNING, "plan"),
    (TaskState.TOOL_EXECUTING, "execute"),
    (TaskState.OBSERVING, "observe"),
    (TaskState.VERIFYING, "hand over to verifier"),
)


def test_task_traverses_state_machine_with_cache_and_async_storage(tmp_path: Path) -> None:
    db_file = tmp_path / "pipeline.db"
    database = Database(f"sqlite:///{db_file}")
    database.create_all()

    broker = InMemoryBroker()
    cache = StateCache(InMemoryStateBackend(), ttl_seconds=300)
    observed: list[str] = []

    with database.session_factory() as session:
        repository = TaskRepository(session, cache)
        task, created = repository.create(
            CreateTask(
                owner_id="owner-1",
                goal="pipeline",
                path="out.txt",
                content="hello",
                idempotency_key="pipeline-1",
            )
        )
        assert created is True
        task_id = task.id

        # 1. Enqueue: SQL is the truth, the broker only carries the wake-up.
        queue = DurableQueue(session, broker, cache)
        job = queue.enqueue(task)
        assert job.status == "QUEUED"
        assert broker.pending() == [task_id]
        assert cache.get(task_id) == TaskState.QUEUED.value
        observed.append(task.status)

        # 2. The woken worker still has to win the SQL claim.
        assert broker.wait(0.01) == task_id
        claimed = queue.claim("worker-1", 30)
        assert claimed is not None and claimed.task_id == task_id

        # 3. Every accepted transition publishes to the cache after commit.
        for target, reason in WORKER_PATH:
            repository.transition(task, target, reason, "worker")
            session.commit()
            assert cache.get(task_id) == target.value
            assert read_state(session, task_id, cache) == target.value
            observed.append(task.status)

        # 4. DONE stays out of reach of everything but the Verifier capability.
        assert (TaskState.VERIFYING, TaskState.DONE) in VERIFIER_ONLY_TRANSITIONS
        with pytest.raises(InvalidTransition):
            repository.transition(task, TaskState.DONE, "cheat", "worker")
        session.rollback()
        assert cache.get(task_id) != TaskState.DONE.value

    assert observed == [
        TaskState.QUEUED.value,
        TaskState.PLANNING.value,
        TaskState.TOOL_EXECUTING.value,
        TaskState.OBSERVING.value,
        TaskState.VERIFYING.value,
    ]

    async def read_through_async_layer() -> None:
        engine = build_async_engine(f"sqlite:///{db_file}")
        try:
            async with get_session(engine) as session:
                task_row = await session.get(TaskFlow, task_id)
                assert task_row is not None
                assert task_row.status == TaskState.VERIFYING.value

                journal = (
                    await session.scalars(
                        select(StateTransition)
                        .where(StateTransition.task_id == task_id)
                        .order_by(StateTransition.id)
                    )
                ).all()
                accepted = [row.to_state for row in journal if not row.reason.startswith("REJECTED")]
                assert accepted == [
                    TaskState.RECEIVED.value,
                    *observed,
                ], "the journal must record every accepted transition, in order"
                # The repository API refuses DONE before it can ever reach a row,
                # so no DONE lands in the journal without the Verifier capability.
                assert all(row.to_state != TaskState.DONE.value for row in journal)

            # The journal is append-only through the async layer as well.
            async with engine.begin() as connection:
                with pytest.raises(DatabaseError, match="append-only"):
                    await connection.execute(
                        text("UPDATE state_transitions SET to_state='DONE'")
                    )
        finally:
            await engine.dispose()

    asyncio.run(read_through_async_layer())


def test_pipeline_without_redis_is_identical(tmp_path: Path) -> None:
    """redis_url=None: no broker, no cache — the pre-P4.3 path, still complete."""
    database = Database(f"sqlite:///{tmp_path}/plain.db")
    database.create_all()

    with database.session_factory() as session:
        task = TaskFlow(
            owner_id="owner-1",
            goal="plain",
            target_path=".",
            idempotency_key="plain-1",
        )
        session.add(task)
        session.commit()

        queue = DurableQueue(session)
        repository = TaskRepository(session)
        queue.enqueue(task)
        assert queue.claim("worker-1", 30) is not None
        for target, reason in WORKER_PATH:
            repository.transition(task, target, reason, "worker")
        session.commit()

        assert task.status == TaskState.VERIFYING.value
        assert read_state(session, task.id, None) == TaskState.VERIFYING.value


def test_broker_signal_loss_costs_latency_not_work(tmp_path: Path) -> None:
    """A dropped wake-up never loses a task: the SQL claim still finds it."""
    database = Database(f"sqlite:///{tmp_path}/lossy.db")
    database.create_all()
    broker = InMemoryBroker()

    with database.session_factory() as session:
        task = TaskFlow(
            owner_id="owner-1",
            goal="lossy",
            target_path=".",
            idempotency_key="lossy-1",
        )
        session.add(task)
        session.commit()

        DurableQueue(session, broker).enqueue(task)
        # Simulate a lost Redis message.
        asyncio.run(broker.dequeue(timeout=0.01))
        asyncio.run(broker.ack(task.id))
        assert broker.pending() == []

        claimed = DurableQueue(session, broker).claim("worker-1", 30)
        assert claimed is not None and claimed.task_id == task.id


