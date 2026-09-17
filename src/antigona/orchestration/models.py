"""Autonomous Goal Orchestration (M2) — ORM models.

Five tables on the shared ``Base`` (auto-created by ``Database.create_all``):

* ``goals`` — durable Goal (PENDING..SUCCEEDED/FAILED/CANCELLED).
* ``goal_flows`` — durable Flow with optimistic **revision** (CAS on update).
* ``wake_events`` — durable WAIT/WAKE queue (WAIT never burns tokens).
* ``service_health`` — orchestration-level service state (persisted).
* ``service_handoffs`` — durable handoff records (failover audit trail).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from antigona.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(128), index=True, default="owner")
    session_id: Mapped[str] = mapped_column(String(128), default="")
    objective: Mapped[str] = mapped_column(Text)
    workspace: Mapped[str] = mapped_column(Text, default="")
    mutation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    test_command: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    max_cycles: Mapped[int] = mapped_column(Integer, default=3)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoalFlow(Base):
    __tablename__ = "goal_flows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    goal_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PLANNED", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_stage: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WakeEvent(Base):
    __tablename__ = "wake_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceHealthState(Base):
    __tablename__ = "service_health"

    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    capacity: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_ok: Mapped[bool | None] = mapped_column(default=None)
    observed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ServiceHandoff(Base):
    __tablename__ = "service_handoffs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    goal_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_worker: Mapped[str] = mapped_column(String(64))
    replacement_worker: Mapped[str] = mapped_column(String(64))
    reason_for_handoff: Mapped[str] = mapped_column(Text)
    handoff_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
