"""add operations index for Operation progress-tracking model

Revision ID: 0002_operations
Revises: 0001_initial
Create Date: 2026-07-28

The ``operations`` table itself is **not** created here. ``0001_initial``
deliberately emits its schema from the single shared ``Base.metadata`` via
``metadata.create_all(bind=bind, checkfirst=True)`` (see that migration's
docstring / ADR-0014: "no schema drift" by construction) — so the moment
:class:`~antigona.durable.operation_models.Operation` is registered on
``Base``, ``0001_initial`` already creates ``operations`` with the correct
columns, on every dialect, with zero code here.

An earlier version of this migration also called ``op.create_table(...)``
for ``operations`` explicitly. Against a fresh database that raises
``OperationalError: table operations already exists``, since 0001 already
created it moments before this migration runs — reproduced directly via
``tests/unit/test_storage.py::test_alembic_upgrade_head_creates_schema_and_triggers``
combined with any test that imports ``antigona.durable.operation_models``.
This migration now only adds the one thing ``create_all`` does *not* cover:
the composite ``(chat_id, status)`` index (no ``__table_args__`` on
``Operation`` declares it, only per-column ``index=True``).

Coexists with ``durable_operations`` (the original DurableOperation model);
the two tables serve different state-machine lifecycles and are *not* meant
to be merged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from antigona.storage.models import metadata  # noqa: F401 — kept warm for env.py

revision: str = "0002_operations"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = ("0001_initial",)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("operations")}
    if "ix_operations_chat_status" not in existing_indexes:
        op.create_index("ix_operations_chat_status", "operations", ["chat_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_operations_chat_status", table_name="operations")
