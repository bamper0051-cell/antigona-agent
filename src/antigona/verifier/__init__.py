"""Verifier package for Antigona (v2).

Provides LLM-Judge verification on a secondary model and trajectory monitoring
for anti-reward-hacking protection.
"""

from __future__ import annotations

from .anomaly import detect_trajectory_anomalies
from .criteria import MissingVerifierCriteria, VerifierCriteriaDatabase, VerifierCriteriaStore
from .judge import (
    HTTPVerifierProvider,
    LLMJudge,
    ModelCollisionError,
    ProviderMalformedResponse,
    ProviderModelMismatch,
    ProviderResult,
    ProviderTransportError,
    VerifierProvider,
)
from .skill_checks import (
    DEFAULT_ALLOWED_TOOLS,
    SkillVerificationError,
    verify_skill_card_for_promotion,
)

__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "HTTPVerifierProvider",
    "LLMJudge",
    "MissingVerifierCriteria",
    "ModelCollisionError",
    "ProviderMalformedResponse",
    "ProviderModelMismatch",
    "ProviderResult",
    "ProviderTransportError",
    "SkillVerificationError",
    "VerifierCriteriaDatabase",
    "VerifierCriteriaStore",
    "VerifierProvider",
    "detect_trajectory_anomalies",
    "verify_skill_card_for_promotion",
]

