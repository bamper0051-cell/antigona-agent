"""Durable Execution Kernel (M1) — ORM models.

Four tables on the shared ``Base`` (auto-created by ``Database.create_all``):

* ``kernel_tasks`` — logical work (Task). Retry policy, idempotency key,
  cancellation flag, one active lease.
* ``kernel_runs`` — one execution attempt per row (Run). ``(task_id,
  run_number)`` unique => a task never has two active Runs; each attempt is
  its own durable record with lease + heartbeat + result.
* ``kernel_dependencies`` — child/parent edges (a child stays BLOCKED until
  all parents SUCCEEDED).
* ``kernel_transitions`` — append-only machine-readable history of every
  task/run state change.

Runtime truth lives here (durable storage), never in the LLM or a worker
process. Worker/LLM are stateless executors of Runs held in these tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import utcnow
from .state import RunState, TaskState


def _uuid() -> str:
    return str(uuid.uuid4())


class KernelTask(Base):
    __tablename__ = "kernel_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(64), default="generic")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(32), default=TaskState.PENDING.value, index=True
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # One active lease on the task (mutual exclusion across workers).
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # Idempotency: a non-empty key is unique per owner. SQLite-only partial
        # index so the empty default never collides.
        Index(
            "uq_kernel_tasks_owner_idem",
            "owner_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key != ''"),
        ),
    )


class KernelRun(Base):
    __tablename__ = "kernel_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("kernel_tasks.id", ondelete="CASCADE"), index=True
    )
    run_number: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        String(32), default=RunState.READY.value, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("task_id", "run_number", name="uq_kernel_run_task_number"),
    )


class KernelDependency(Base):
    __tablename__ = "kernel_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    child_id: Mapped[str] = mapped_column(
        ForeignKey("kernel_tasks.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[str] = mapped_column(
        ForeignKey("kernel_tasks.id", ondelete="CASCADE"), index=True
    )

    __table_args__ = (
        UniqueConstraint("child_id", "parent_id", name="uq_kernel_dep_child_parent"),
    )


class KernelTransition(Base):
    __tablename__ = "kernel_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(8))  # 'task' | 'run'
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="kernel")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "KernelTask",
    "KernelRun",
    "KernelDependency",
    "KernelTransition",
]
