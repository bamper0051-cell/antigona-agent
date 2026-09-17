"""initial durable schema (equivalent of SCHEMA_VERSION 6) + append-only journals

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-26

The schema itself is emitted from the single shared ``Base.metadata`` rather than
being transcribed by hand: the sync layer's ``create_all`` and this migration are
then structurally identical by construction, which is exactly the schema-drift
risk ADR-0014 calls out. On top of that the append-only journals get their
dialect-specific guards (PL/pgSQL ``RAISE EXCEPTION`` triggers on Postgres,
``RAISE(ABORT)`` triggers on SQLite).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from antigona.storage.engine import (
    APPEND_ONLY_TABLES,
    postgres_append_only_ddl,
    sqlite_append_only_ddl,
)
from antigona.storage.models import SCHEMA_VERSION, metadata

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind, checkfirst=True)
    op.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            "INSERT INTO schema_version(version, applied_at) "
            f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
        )
        statements = postgres_append_only_ddl()
    else:
        op.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) "
            f"VALUES ({SCHEMA_VERSION}, CURRENT_TIMESTAMP)"
        )
        statements = sqlite_append_only_ddl()
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS antigona_append_only()")
    else:
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
    op.execute("DROP TABLE IF EXISTS schema_version")
    metadata.drop_all(bind=bind, checkfirst=True)
