"""Hermes RCA — RCAResult contract.

Structured output returned after Hermes diagnoses an ErrorEnvelope.
Reuses the existing ``Evidence`` shape from ``antigona.contracts`` and follows
the ``VerificationVerdict`` model precedent in ``durable/execution_models``.
Confidence levels: LOW / MEDIUM / HIGH / CONFIRMED (CONFIRMED only with direct
evidence, per spec section 8D).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from antigona.contracts import Evidence


class RCAConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CONFIRMED = "CONFIRMED"


class RCAStatus(StrEnum):
    IDLE = "IDLE"
    CAPTURING = "CAPTURING"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSED = "DIAGNOSED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass
class RCAResult:
    """Final structured RCA output (spec section 9)."""

    rca_id: str
    error_id: str
    correlation_id: str
    category: str = "UNKNOWN"
    summary: str = ""
    root_cause: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    confidence: RCAConfidence = RCAConfidence.LOW
    affected_components: list[str] = field(default_factory=list)
    user_impact: str = ""
    recommended_actions: list[str] = field(default_factory=list)
    safe_to_auto_fix: bool = False
    requires_owner_approval: bool = True
    suggested_patch: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    analysis_duration_ms: float = 0.0
    status: RCAStatus = RCAStatus.DIAGNOSED
    hermes_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = str(self.confidence.value)
        data["status"] = str(self.status.value)
        data["evidence"] = [asdict(e) for e in self.evidence]
        return data
