"""Alembic environment for the async durable layer.

Adapted from the official ``alembic init -t async`` template (MIT). Two
deliberate deviations:

* ``fileConfig`` is **not** called — migrations must never reconfigure the
  process-wide logging that :mod:`antigona.observability` owns.
* ``target_metadata`` is the one shared ``Base.metadata`` from
  :mod:`antigona.models`, so Postgres (Alembic) and SQLite (``create_all``) can
  never diverge.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from antigona.storage.models import metadata as target_metadata

config = context.config


def run_migrations_offline() -> None:
    """Emit SQL for the configured URL without connecting to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations through an ``AsyncEngine`` built from the Alembic config."""
    section = config.get_section(config.config_ini_section, {})
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
