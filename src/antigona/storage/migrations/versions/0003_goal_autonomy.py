"""add structured M2 autonomy fields

Revision ID: 0003_goal_autonomy
Revises: 0002_operations
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_goal_autonomy"
down_revision: str | None = "0002_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = _goal_columns()
    if columns is None:
        return
    additions = {
        "workspace": sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        "mutation_required": sa.Column(
            "mutation_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        "test_command": sa.Column("test_command", sa.JSON(), nullable=False, server_default="[]"),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("goals", column)


def downgrade() -> None:
    if _goal_columns() is None:
        return
    for name in ("test_command", "mutation_required", "workspace"):
        op.drop_column("goals", name)


def _goal_columns() -> set[str] | None:
    """Inspect the optional M2 table without retaining an Inspector connection."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        exists = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'")
        ).first()
        if exists is None:
            return None
        return {str(row[1]) for row in bind.exec_driver_sql("PRAGMA table_info(goals)")}
    if bind.dialect.name == "postgresql":
        exists = bind.execute(sa.text("SELECT to_regclass('public.goals')")).scalar()
        if exists is None:
            return None
        rows = bind.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'goals'"
        ))
        return {str(row[0]) for row in rows}
    inspector = sa.inspect(bind)
    if "goals" not in inspector.get_table_names():
        return None
    return {str(column["name"]) for column in inspector.get_columns("goals")}
