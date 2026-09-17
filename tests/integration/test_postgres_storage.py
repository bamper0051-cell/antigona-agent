"""Real Postgres checks for the P4.3 async storage layer.

Skipped unless ``ANTIGONA_TEST_POSTGRES_URL`` is set — the same pattern as
``test_postgres_memory.py``. CI never spawns a database; these tests exist so a
Postgres deployment can be validated on demand (Alembic head, CRUD, and the
append-only triggers that replace SQLite's ``RAISE(ABORT)`` guards).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from antigona.storage import build_async_engine, get_session, normalize_db_url
from antigona.storage.migrator import head_revision, upgrade_to_head
from antigona.storage.models import StateTransition, TaskFlow

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTIGONA_TEST_POSTGRES_URL"),
    reason="ANTIGONA_TEST_POSTGRES_URL is required for real Postgres storage tests",
)


def _url() -> str:
    return normalize_db_url(os.environ["ANTIGONA_TEST_POSTGRES_URL"])


def test_alembic_upgrade_head_then_crud_and_append_only() -> None:
    upgrade_to_head(_url())
    assert head_revision() == "0001_initial"

    owner = f"owner-{uuid.uuid4()}"

    async def scenario() -> None:
        engine = build_async_engine(_url())
        try:
            async with get_session(engine) as session:
                task = TaskFlow(
                    owner_id=owner,
                    goal="postgres crud",
                    target_path="out.txt",
                    idempotency_key=str(uuid.uuid4()),
                )
                session.add(task)
                await session.flush()
                session.add(
                    StateTransition(
                        task_id=task.id,
                        entity_id=task.id,
                        entity_type="task",
                        from_state=None,
                        to_state="RECEIVED",
                        reason="created",
                        actor="test",
                        correlation_id=str(uuid.uuid4()),
                    )
                )
                await session.commit()
                task_id = task.id

            async with get_session(engine) as session:
                loaded = await session.scalar(select(TaskFlow).where(TaskFlow.id == task_id))
                assert loaded is not None and loaded.goal == "postgres crud"
                revision = await session.execute(text("SELECT version_num FROM alembic_version"))
                assert revision.scalar() == "0001_initial"

            # The PL/pgSQL triggers make the journal append-only on Postgres too.
            async with engine.connect() as connection:
                with pytest.raises(DatabaseError, match="append-only"):
                    await connection.execute(
                        text("UPDATE state_transitions SET to_state='DONE' WHERE task_id=:t"),
                        {"t": task_id},
                    )
                await connection.rollback()
                with pytest.raises(DatabaseError, match="append-only"):
                    await connection.execute(
                        text("DELETE FROM state_transitions WHERE task_id=:t"), {"t": task_id}
                    )
                await connection.rollback()

            # No cleanup on purpose: the journal is append-only by design, so the
            # rows this test wrote are expected to survive it (each run uses a
            # fresh random owner id).
        finally:
            await engine.dispose()

    asyncio.run(scenario())
