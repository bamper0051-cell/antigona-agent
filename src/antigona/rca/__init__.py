"""Hermes RCA — root-cause analysis diagnostic layer for Antigona CLI.

Hermes observes, diagnoses and explains. Antigona owns execution, safety and
control. All Hermes behaviour is read-only; remediation is proposed as data
only and requires separate Antigona owner approval.
"""

from __future__ import annotations

from antigona.rca.dedup import Deduplicator, fingerprint
from antigona.rca.envelope import ErrorEnvelope, apply_redaction
from antigona.rca.events import EVENT_ERROR_DETECTED, HermesRCAConsumer
from antigona.rca.pipeline import RCAEngine, analyze
from antigona.rca.result import RCAConfidence, RCAResult, RCAStatus
from antigona.rca.storage import RCARepository

__all__ = [
    "EVENT_ERROR_DETECTED",
    "Deduplicator",
    "ErrorEnvelope",
    "HermesRCAConsumer",
    "RCAConfidence",
    "RCAEngine",
    "RCARepository",
    "RCAResult",
    "RCAStatus",
    "analyze",
    "apply_redaction",
    "fingerprint",
]
