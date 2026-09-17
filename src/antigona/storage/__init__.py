"""Async durable storage (P4.3): Postgres primary, SQLite fallback.

Public surface::

    from antigona.storage import build_async_engine, get_session

The sync layer in :mod:`antigona.database` is untouched and remains the default
for legacy callers — see ``docs/adr/0014-postgres-redis.md`` for the deprecation
path.
"""

from __future__ import annotations

from .engine import (
    APPEND_ONLY_TABLES,
    StorageUnavailableError,
    UnsupportedDatabaseURL,
    build_async_engine,
    connect_with_retries,
    create_all,
    ensure_storage_available,
    is_sqlite_url,
    mask_db_url,
    normalize_db_url,
    postgres_append_only_ddl,
    sqlite_append_only_ddl,
)
from .models import Base, metadata
from .session import SessionFactory, create_session_factory, get_session

__all__ = [
    "APPEND_ONLY_TABLES",
    "Base",
    "SessionFactory",
    "StorageUnavailableError",
    "UnsupportedDatabaseURL",
    "build_async_engine",
    "connect_with_retries",
    "create_all",
    "create_session_factory",
    "ensure_storage_available",
    "get_session",
    "is_sqlite_url",
    "mask_db_url",
    "metadata",
    "normalize_db_url",
    "postgres_append_only_ddl",
    "sqlite_append_only_ddl",
]
