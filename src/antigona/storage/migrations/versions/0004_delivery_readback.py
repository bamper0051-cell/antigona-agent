"""delivery receipt read-back: provider_message_id / read_back_status / read_back_at

Revision ID: 0004_delivery_readback
Revises: 0003_goal_autonomy
Create Date: 2026-09-17

B53 (DELIV-02): a ``delivery_receipts`` row previously recorded only
transmission-level facts, so "delivered" was asserted by the sender and never
confirmed by the provider. This migration adds three NULLABLE columns and
nothing else:

* ``provider_message_id`` — the identifier the provider returned for the message
  (e.g. Telegram ``message_id``).
* ``read_back_status``    — SEND_ACK / UNSUPPORTED / REFUTED
  (see :mod:`antigona.delivery.readback`). SEND_ACK confirms *transmission* only,
  never that a human read the message.
* ``read_back_at``        — when the acknowledgement was obtained.

Existing rows stay valid with no backfill; no existing column is dropped or
renamed. ``0001_initial`` emits its schema from the shared ``Base.metadata`` via
``create_all(checkfirst=True)``, so on a fresh database the columns already
exist when this migration runs — hence the additive column guard, mirroring
``0003_goal_autonomy``. ``downgrade`` drops exactly the columns this migration
could have added.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004_delivery_readback"
down_revision: str | None = "0003_goal_autonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "delivery_receipts"

_ADDITIONS: dict[str, sa.Column[Any]] = {
    "provider_message_id": sa.Column("provider_message_id", sa.String(255), nullable=True),
    "read_back_status": sa.Column("read_back_status", sa.String(16), nullable=True),
    "read_back_at": sa.Column("read_back_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    columns = _receipt_columns()
    if columns is None:
        return
    for name, column in _ADDITIONS.items():
        if name not in columns:
            op.add_column(_TABLE, column)


def downgrade() -> None:
    columns = _receipt_columns()
    if columns is None:
        return
    for name in ("read_back_at", "read_back_status", "provider_message_id"):
        if name in columns:
            op.drop_column(_TABLE, name)


def _receipt_columns() -> set[str] | None:
    """Inspect the optional table without retaining an Inspector connection."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        exists = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": _TABLE},
        ).first()
        if exists is None:
            return None
        return {str(row[1]) for row in bind.exec_driver_sql(f"PRAGMA table_info({_TABLE})")}
    if bind.dialect.name == "postgresql":
        exists = bind.execute(sa.text("SELECT to_regclass('public.delivery_receipts')")).scalar()
        if exists is None:
            return None
        rows = bind.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": _TABLE},
        )
        return {str(row[0]) for row in rows}
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return None
    return {str(column["name"]) for column in inspector.get_columns(_TABLE)}
