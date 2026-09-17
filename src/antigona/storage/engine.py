"""Async engine factory for the durable layer (P4.3).

This module is the async counterpart of :mod:`antigona.database`: the sync layer
stays operational for legacy callers, while every new durable path builds its
engine here. Two dialects are supported — ``postgresql+asyncpg`` (primary) and
``sqlite+aiosqlite`` (default fallback, so behaviour without configuration is
identical to P0..P4.2).

Fail-closed is a property of this module, not of its callers: a database that
cannot be reached is retried with exponential backoff and then raises
:class:`StorageUnavailableError`. There is no code path where a worker keeps
running with a silently-missing durable layer.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..observability import event as log_event

#: URL schemes we accept, mapped onto the async driver actually used.
_ASYNC_SCHEMES: dict[str, str] = {
    "sqlite": "sqlite+aiosqlite",
    "sqlite+pysqlite": "sqlite+aiosqlite",
    "sqlite+aiosqlite": "sqlite+aiosqlite",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "postgresql+psycopg": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "postgresql+asyncpg": "postgresql+asyncpg",
}

#: Credentials must never reach a log line; ``user:password@host`` collapses to
#: ``user:[REDACTED]@host`` before any URL is emitted as an observability field.
_URL_PASSWORD = re.compile(r"(?i)^([a-z0-9+]+://[^:/?#@]+):[^@/]*@")


class StorageUnavailableError(RuntimeError):
    """Raised when the durable layer stays unreachable after every retry."""


class UnsupportedDatabaseURL(ValueError):
    """Raised for a database URL whose driver the async layer cannot serve."""


def normalize_db_url(url: str) -> str:
    """Return ``url`` rewritten onto its async driver.

    ``sqlite:///x.db`` becomes ``sqlite+aiosqlite:///x.db`` and
    ``postgresql://…`` becomes ``postgresql+asyncpg://…``, so a legacy
    ``ANTIGONA_DATABASE_URL`` keeps working unchanged. An unknown driver is a
    configuration error and is refused loudly instead of being coerced.
    """
    candidate = url.strip()
    if "://" not in candidate:
        raise UnsupportedDatabaseURL(f"database url has no scheme: {mask_db_url(url)}")
    scheme, _, remainder = candidate.partition("://")
    target = _ASYNC_SCHEMES.get(scheme.lower())
    if target is None:
        raise UnsupportedDatabaseURL(f"unsupported database driver: {scheme}")
    return f"{target}://{remainder}"


def mask_db_url(url: str) -> str:
    """Return ``url`` with any password in the userinfo replaced by a marker."""
    return _URL_PASSWORD.sub(r"\1:[REDACTED]@", url.strip())


def is_sqlite_url(url: str) -> bool:
    """True when ``url`` targets SQLite (used to pick PRAGMA/trigger handling)."""
    return url.strip().lower().startswith("sqlite")


def _slow_query_threshold(explicit: float | None) -> float:
    threshold = (
        float(os.getenv("ANTIGONA_DB_SLOW_QUERY_MS", "250")) if explicit is None else explicit
    )
    if threshold < 0:
        raise ValueError("slow_query_threshold_ms must be non-negative")
    return threshold


def _duration_ms(context: object | None) -> float:
    started = getattr(context, "_antigona_query_started", time.monotonic())
    return round((time.monotonic() - started) * 1000, 3)


def _install_instrumentation(engine: AsyncEngine, threshold_ms: float) -> None:
    """Mirror the sync layer's slow-query instrumentation onto the async engine.

    SQLAlchemy emits DBAPI-level events on the underlying *sync* engine even for
    an ``AsyncEngine``, so the listeners are registered there.
    """
    sync_engine = engine.sync_engine

    @sqlalchemy_event.listens_for(sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        _conn: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        context: Any,
        _executemany: bool,
    ) -> None:
        context._antigona_query_started = time.monotonic()

    @sqlalchemy_event.listens_for(sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        _conn: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        context: Any,
        _executemany: bool,
    ) -> None:
        duration_ms = _duration_ms(context)
        log_event(
            "storage.query.completed",
            service="storage",
            correlation_id=None,
            status="slow" if duration_ms >= threshold_ms else "ok",
            duration_ms=duration_ms,
        )

    @sqlalchemy_event.listens_for(sync_engine, "handle_error")
    def handle_error(exception_context: ExceptionContext) -> None:
        log_event(
            "storage.query.failed",
            service="storage",
            correlation_id=None,
            status="error",
            duration_ms=_duration_ms(exception_context.execution_context),
            error_type=type(exception_context.original_exception).__name__,
        )


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    @sqlalchemy_event.listens_for(engine.sync_engine, "connect")
    def configure(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def build_async_engine(
    db_url: str,
    *,
    slow_query_threshold_ms: float | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Build the async engine for ``db_url`` with P0 semantics preserved.

    The URL is normalized onto its async driver first, so callers may pass a
    legacy sync URL. SQLite keeps the P0 connection tuning (foreign keys, WAL),
    Postgres relies on Alembic for its schema and append-only triggers.
    """
    url = normalize_db_url(db_url)
    threshold_ms = _slow_query_threshold(slow_query_threshold_ms)
    connect_args: dict[str, Any] = {"timeout": 30} if is_sqlite_url(url) else {}
    engine = create_async_engine(url, echo=echo, connect_args=connect_args)
    _install_instrumentation(engine, threshold_ms)
    if is_sqlite_url(url):
        _install_sqlite_pragmas(engine)
    return engine


async def connect_with_retries(
    engine: AsyncEngine,
    *,
    retries: int = 5,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Probe the durable layer, retrying with exponential backoff, then fail closed.

    Raises :class:`StorageUnavailableError` once ``retries`` attempts are spent.
    Callers (worker/gateway startup) must let that exception terminate the
    process: running without the durable layer would lose state silently, which
    the P4.3 fail-closed invariant forbids.
    """
    attempts = max(1, retries)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            last_error = exc
            log_event(
                "storage.connect.retry",
                service="storage",
                correlation_id=None,
                status="degraded",
                attempt=attempt,
                attempts=attempts,
                url=mask_db_url(str(engine.url)),
                error_type=type(exc).__name__,
            )
            if attempt < attempts:
                await sleep(backoff_seconds * (2 ** (attempt - 1)))
            continue
        log_event(
            "storage.connect.ready",
            service="storage",
            correlation_id=None,
            status="ok",
            attempt=attempt,
            url=mask_db_url(str(engine.url)),
        )
        return
    log_event(
        "storage.connect.failed",
        service="storage",
        correlation_id=None,
        status="error",
        attempts=attempts,
        url=mask_db_url(str(engine.url)),
    )
    raise StorageUnavailableError(
        f"database unreachable after {attempts} attempt(s): {mask_db_url(str(engine.url))}"
    ) from last_error


async def _probe(db_url: str, retries: int, backoff_seconds: float) -> None:
    engine = build_async_engine(db_url)
    try:
        await connect_with_retries(engine, retries=retries, backoff_seconds=backoff_seconds)
    finally:
        await engine.dispose()


def ensure_storage_available(
    db_url: str,
    *,
    retries: int = 5,
    backoff_seconds: float = 1.0,
) -> None:
    """Sync startup gate for the worker/gateway: reachable, or refuse to start.

    Raises :class:`StorageUnavailableError` — the caller must let it propagate so
    the process exits instead of running without a durable layer.
    """
    asyncio.run(_probe(db_url, retries, backoff_seconds))


#: Journals that may only ever grow. Enforced by triggers in both dialects.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "state_transitions",
    "skill_transitions",
    "schedule_events",
)


def sqlite_append_only_ddl() -> tuple[str, ...]:
    """SQLite trigger DDL protecting the append-only journals (P0 parity)."""
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        for verb in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} "
                f"BEFORE {verb} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
    return tuple(statements)


def postgres_append_only_ddl() -> tuple[str, ...]:
    """PostgreSQL function/trigger DDL protecting the append-only journals."""
    statements: list[str] = [
        "CREATE OR REPLACE FUNCTION antigona_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql"
    ]
    for table in APPEND_ONLY_TABLES:
        statements.append(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        statements.append(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION antigona_append_only()"
        )
    return tuple(statements)


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema on the fallback (SQLite) path.

    Postgres deployments must go through Alembic (``antigona db upgrade``); this
    helper keeps the SQLite default working exactly as it did before P4.3 and is
    a no-op DDL-wise for tables that already exist.
    """
    from .models import SCHEMA_VERSION, Base

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        if engine.dialect.name == "sqlite":
            await connection.exec_driver_sql(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) "
                f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP)"
            )
            for statement in sqlite_append_only_ddl():
                await connection.exec_driver_sql(statement)
        else:
            await connection.exec_driver_sql(
                "INSERT INTO schema_version(version, applied_at) "
                f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            )
            for statement in postgres_append_only_ddl():
                await connection.exec_driver_sql(statement)
