"""Unit tests for the P4.3 async storage layer.

No real Postgres and no real network: the fallback driver (``sqlite+aiosqlite``)
covers the happy paths and a fake engine covers the fail-closed retry path.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError, OperationalError

from antigona.storage import (
    StorageUnavailableError,
    UnsupportedDatabaseURL,
    build_async_engine,
    connect_with_retries,
    create_all,
    ensure_storage_available,
    get_session,
    mask_db_url,
    normalize_db_url,
)
from antigona.storage.migrator import head_revision, upgrade_to_head
from antigona.storage.models import SCHEMA_VERSION, StateTransition, TaskFlow

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


def test_engine_normalizes_legacy_urls() -> None:
    assert normalize_db_url("sqlite:///./antigona.db") == "sqlite+aiosqlite:///./antigona.db"
    assert normalize_db_url("sqlite+pysqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert normalize_db_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("postgresql+psycopg://u@h/db") == "postgresql+asyncpg://u@h/db"
    # Already-async URLs are idempotent.
    assert normalize_db_url(MEMORY_URL) == MEMORY_URL


def test_engine_rejects_unknown_driver() -> None:
    with pytest.raises(UnsupportedDatabaseURL):
        normalize_db_url("mysql+aiomysql://u@h/db")
    with pytest.raises(UnsupportedDatabaseURL):
        normalize_db_url("just-a-path.db")


def test_mask_db_url_never_leaks_password() -> None:
    masked = mask_db_url("postgresql+asyncpg://antigona:s3cr3t@db:5432/antigona")
    assert "s3cr3t" not in masked
    assert masked == "postgresql+asyncpg://antigona:[REDACTED]@db:5432/antigona"
    # URLs without credentials survive untouched.
    assert mask_db_url(MEMORY_URL) == MEMORY_URL


def test_engine_builds_for_aiosqlite_memory() -> None:
    async def scenario() -> None:
        engine = build_async_engine("sqlite:///:memory:", slow_query_threshold_ms=0.0)
        try:
            assert engine.dialect.name == "sqlite"
            assert str(engine.url).startswith("sqlite+aiosqlite")
            async with engine.connect() as connection:
                foreign_keys = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert foreign_keys.scalar() == 1
                assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_engine_rejects_negative_slow_query_threshold() -> None:
    with pytest.raises(ValueError):
        build_async_engine(MEMORY_URL, slow_query_threshold_ms=-1.0)


def test_async_session_crud_taskflow() -> None:
    async def scenario() -> None:
        engine = build_async_engine(MEMORY_URL)
        try:
            await create_all(engine)
            async with get_session(engine) as session:
                task = TaskFlow(
                    owner_id="owner-1",
                    goal="async crud",
                    target_path="notes.txt",
                    idempotency_key="storage-crud",
                )
                session.add(task)
                await session.commit()
                task_id = task.id

            async with get_session(engine) as session:
                loaded = await session.get(TaskFlow, task_id)
                assert loaded is not None
                assert loaded.goal == "async crud"
                assert loaded.status == "RECEIVED"
                loaded.goal = "async crud v2"
                await session.commit()

            async with get_session(engine) as session:
                again = await session.scalar(select(TaskFlow).where(TaskFlow.id == task_id))
                assert again is not None and again.goal == "async crud v2"
                version = await session.execute(text("SELECT version FROM schema_version"))
                assert version.scalar() == SCHEMA_VERSION
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_append_only_triggers_hold_on_sqlite() -> None:
    async def scenario() -> None:
        engine = build_async_engine(MEMORY_URL)
        try:
            await create_all(engine)
            async with get_session(engine) as session:
                task = TaskFlow(
                    owner_id="owner-1",
                    goal="journal",
                    target_path=".",
                    idempotency_key="append-only",
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
                        correlation_id="corr-1",
                    )
                )
                await session.commit()

            async with engine.begin() as connection:
                with pytest.raises(DatabaseError, match="append-only"):
                    await connection.exec_driver_sql(
                        "UPDATE state_transitions SET to_state='DONE'"
                    )
            async with engine.begin() as connection:
                with pytest.raises(DatabaseError, match="append-only"):
                    await connection.exec_driver_sql("DELETE FROM state_transitions")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


class _FailingEngine:
    """Minimal AsyncEngine stand-in whose every connect attempt fails."""

    url = "postgresql+asyncpg://antigona:s3cr3t@nope.invalid/antigona"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def connect(self) -> object:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return _OkConnection()


class _OkConnection:
    async def __aenter__(self) -> _OkConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _statement: object) -> object:
        return object()


def test_pg_unavailable_retries_then_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    engine = _FailingEngine(failures=99)

    async def scenario() -> None:
        with pytest.raises(StorageUnavailableError):
            await connect_with_retries(
                engine,  # type: ignore[arg-type]
                retries=3,
                backoff_seconds=0.5,
                sleep=fake_sleep,
            )

    with caplog.at_level("INFO", logger="antigona"):
        asyncio.run(scenario())

    assert engine.attempts == 3, "every configured attempt must be spent before failing"
    assert delays == [0.5, 1.0], "backoff must grow exponentially between attempts"
    assert "storage.connect.failed" in caplog.text
    assert "s3cr3t" not in caplog.text, "credentials must never reach the log"


def test_connect_retries_recover_from_transient_outage() -> None:
    engine = _FailingEngine(failures=2)

    async def fake_sleep(_delay: float) -> None:
        return None

    asyncio.run(
        connect_with_retries(
            engine,  # type: ignore[arg-type]
            retries=5,
            backoff_seconds=0.01,
            sleep=fake_sleep,
        )
    )
    assert engine.attempts == 3


def test_ensure_storage_available_gate(tmp_path: object) -> None:
    ensure_storage_available(MEMORY_URL, retries=1, backoff_seconds=0.0)
    with pytest.raises(UnsupportedDatabaseURL):
        ensure_storage_available("mysql://u@h/db", retries=1, backoff_seconds=0.0)


def test_settings_derive_async_db_url_from_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from antigona.config import Settings

    monkeypatch.delenv("ANTIGONA_DB_URL", raising=False)
    monkeypatch.delenv("ANTIGONA_REDIS_URL", raising=False)
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", "sqlite:///./legacy.db")

    settings = Settings.from_env()
    assert settings.database_url == "sqlite:///./legacy.db"
    assert settings.db_url == "sqlite+aiosqlite:///./legacy.db"
    assert settings.async_db_url() == "sqlite+aiosqlite:///./legacy.db"
    # Redis off by default → pre-P4.3 behaviour.
    assert settings.redis_url is None
    assert settings.redis_state_ttl_seconds == 300
    assert settings.db_connect_retries == 5
    assert settings.db_connect_backoff_seconds == 1.0


def test_settings_accept_explicit_db_and_redis_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    from antigona.config import Settings

    monkeypatch.setenv("ANTIGONA_DB_URL", "postgresql://antigona@db:5432/antigona")
    monkeypatch.setenv("ANTIGONA_REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setenv("ANTIGONA_REDIS_STATE_TTL", "60")
    monkeypatch.setenv("ANTIGONA_DB_CONNECT_RETRIES", "2")
    monkeypatch.setenv("ANTIGONA_DB_CONNECT_BACKOFF", "0.25")

    settings = Settings.from_env()
    assert settings.db_url == "postgresql+asyncpg://antigona@db:5432/antigona"
    assert settings.redis_url == "redis://cache:6379/0"
    assert settings.redis_state_ttl_seconds == 60
    assert settings.db_connect_retries == 2
    assert settings.db_connect_backoff_seconds == 0.25


def test_settings_reject_unknown_db_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    from antigona.config import Settings

    monkeypatch.setenv("ANTIGONA_DB_URL", "mysql://root@db/antigona")
    with pytest.raises(RuntimeError, match="invalid database url"):
        Settings.from_env()


def test_settings_default_url_is_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    from antigona.config import Settings

    monkeypatch.delenv("ANTIGONA_DB_URL", raising=False)
    monkeypatch.delenv("ANTIGONA_DATABASE_URL", raising=False)
    assert Settings.from_env().db_url == "sqlite+aiosqlite:///./antigona.db"


def test_alembic_upgrade_head_creates_schema_and_triggers(tmp_path: object) -> None:
    db_path = f"{tmp_path}/alembic.db"
    masked = upgrade_to_head(f"sqlite:///{db_path}")

    assert masked.startswith("sqlite+aiosqlite:///")
    assert head_revision() == "0003_goal_autonomy"

    connection = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"task_flows", "queue_jobs", "state_transitions", "alembic_version"} <= tables
        assert "operations" in tables, "operations table should exist after 0002 migration"
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "state_transitions_no_update" in triggers
        assert "schedule_events_no_delete" in triggers
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        assert version[0] == SCHEMA_VERSION
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision[0] == "0003_goal_autonomy"
    finally:
        connection.close()
