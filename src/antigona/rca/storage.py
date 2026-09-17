"""Hermes RCA — storage integration (spec section 15).

Reuses the existing storage abstraction: models are declared on the shared
``Base.metadata`` (the same metadata used by sync/async layers and Alembic), so
``Database.create_all`` creates the tables and there is NO separate database.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from antigona.database import Base
from antigona.models import utcnow
from antigona.rca.envelope import ErrorEnvelope
from antigona.rca.result import RCAResult


class RCAErrorRecord(Base):
    """Persisted error + RCA result (spec section 15: error, result, evidence,
    correlation metadata, timestamps, status)."""

    __tablename__ = "rca_error_records"

    rca_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    error_id: Mapped[str] = mapped_column(String(40), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    source_component: Mapped[str] = mapped_column(String(64), index=True, default="")
    category: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    confidence: Mapped[str] = mapped_column(String(16), default="LOW")
    status: Mapped[str] = mapped_column(String(16), default="DIAGNOSED")
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, default="")
    duplicate_count: Mapped[int] = mapped_column(Integer, default=1)
    exception_type: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    user_impact: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    affected_components: Mapped[str] = mapped_column(Text, default="[]")
    recommended_actions: Mapped[str] = mapped_column(Text, default="[]")
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    suggested_patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_to_auto_fix: Mapped[bool] = mapped_column(default=False)
    requires_owner_approval: Mapped[bool] = mapped_column(default=True)
    hermes_available: Mapped[bool] = mapped_column(default=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    flow_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    git_revision: Mapped[str] = mapped_column(String(64), default="")
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diagnostic_hints: Mapped[str] = mapped_column(Text, default="[]")
    analysis_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at = mapped_column(DateTime(timezone=True), default=utcnow)


class RCARepository:
    """Small repository over the shared DB session factory."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    def save(self, result: RCAResult, envelope: ErrorEnvelope, fingerprint: str = "", duplicate_count: int = 1) -> None:
        record = RCAErrorRecord(
            rca_id=result.rca_id,
            error_id=result.error_id,
            correlation_id=result.correlation_id,
            source_component=envelope.source_component,
            category=result.category,
            severity=envelope.severity,
            confidence=str(result.confidence.value),
            status=str(result.status.value),
            fingerprint=fingerprint,
            duplicate_count=duplicate_count,
            exception_type=envelope.exception_type,
            error_message=envelope.error_message,
            root_cause=result.root_cause,
            user_impact=result.user_impact,
            summary=result.summary,
            affected_components=json.dumps(result.affected_components),
            recommended_actions=json.dumps(result.recommended_actions),
            evidence=json.dumps([{"kind": e.kind, "value": e.value} for e in result.evidence]),
            suggested_patch=result.suggested_patch,
            safe_to_auto_fix=result.safe_to_auto_fix,
            requires_owner_approval=result.requires_owner_approval,
            hermes_available=result.hermes_available,
            tool_name=envelope.tool_name,
            provider=envelope.provider,
            model=envelope.model,
            task_id=envelope.task_id,
            flow_id=envelope.flow_id,
            step_id=envelope.step_id,
            session_id=envelope.session_id,
            git_revision=envelope.git_revision,
            runtime_metadata=envelope.runtime_metadata,
            diagnostic_hints=json.dumps(envelope.diagnostic_hints),
            analysis_duration_ms=result.analysis_duration_ms,
        )
        with self._sf() as session:
            session.add(record)
            session.commit()

    def latest(self, limit: int = 10) -> list[RCAErrorRecord]:
        with self._sf() as session:
            rows = list(
                session.query(RCAErrorRecord)
                .order_by(RCAErrorRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            session.expunge_all()
            return rows

    def by_correlation(self, correlation_id: str, limit: int = 50) -> list[RCAErrorRecord]:
        with self._sf() as session:
            rows = list(
                session.query(RCAErrorRecord)
                .filter(RCAErrorRecord.correlation_id == correlation_id)
                .order_by(RCAErrorRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            session.expunge_all()
            return rows

    def by_error_id(self, error_id: str) -> RCAErrorRecord | None:
        """Fetch a single persisted RCA record by its error_id (or None)."""
        with self._sf() as session:
            row = session.query(RCAErrorRecord).filter(
                RCAErrorRecord.error_id == error_id
            ).first()
            if row is None:
                return None
            session.expunge(row)
            record: RCAErrorRecord = row
            return record


    def by_rca_id(self, rca_id: str) -> RCAErrorRecord | None:
        """Fetch a single persisted RCA record by its rca_id (or None)."""
        with self._sf() as session:
            row = session.query(RCAErrorRecord).filter(
                RCAErrorRecord.rca_id == rca_id
            ).first()
            if row is None:
                return None
            session.expunge(row)
            record: RCAErrorRecord = row
            return record


def get_repository(db_path: str | None = None) -> RCARepository:
    """Build an RCARepository bound to the runtime DB (reuses existing storage).

    Uses the same ``Database``/session machinery as the rest of Antigona, so the
    ``rca_error_records`` table is created by the shared ``create_all`` and there
    is no separate database (spec section 15). Lazy import keeps brain startup cheap.
    """
    from antigona.core import paths
    from antigona.database import Database

    resolved = db_path or str(paths.database_path())
    database = Database(resolved)
    database.create_all()
    return RCARepository(database.session_factory)
