"""Skills registry and the ``ASKILL/1`` card format.

Backward-compatible facade: ``from antigona.skills import SkillsRegistry, SkillNotFound``
keeps working exactly as it did when this package was a single module.
"""

from __future__ import annotations

from .canonical import body_digest, canonicalize, footer_line, is_sha256_hex, verify_footer
from .capture import CaptureEngine, capture_from_flow, trajectory_trust
from .errors import (
    SkillCriteriaError,
    SkillEncodingError,
    SkillFormatError,
    SkillHashError,
    SkillIntegrityError,
    SkillLimitError,
    SkillNotFound,
    SkillOrderError,
    SkillSlotError,
    SkillSyntaxError,
    SkillUnknownError,
    SkillVersionError,
)
from .format import parse_card, render_card
from .lifecycle import (
    SKILL_TRANSITIONS,
    TERMINAL_SKILL_STATES,
    VERIFIER_ONLY_SKILL_TRANSITIONS,
    InvalidTransition,
    SkillState,
    check_skill_transition,
)
from .matcher import MatchResult, SkillMatcher, evaluate_card_rules, match_skills
from .records import (
    CardValue,
    Claim,
    ClaimKind,
    HeredocText,
    MatchKind,
    MatchMode,
    MatchRule,
    Origin,
    PlanStep,
    Require,
    RiskCeiling,
    SkillCard,
    SkillRisk,
    SkillTrust,
    Slot,
    SlotType,
    Trust,
    Verdict,
)
from .registry import SkillsRegistry
from .store import CardStore, card_body_path, read_body_safely, write_body_atomically

__all__ = [
    "SKILL_TRANSITIONS",
    "TERMINAL_SKILL_STATES",
    "VERIFIER_ONLY_SKILL_TRANSITIONS",
    "CaptureEngine",
    "CardStore",
    "CardValue",
    "Claim",
    "ClaimKind",
    "HeredocText",
    "InvalidTransition",
    "MatchKind",
    "MatchMode",
    "MatchResult",
    "MatchRule",
    "Origin",
    "PlanStep",
    "Require",
    "RiskCeiling",
    "SkillCard",
    "SkillCriteriaError",
    "SkillEncodingError",
    "SkillFormatError",
    "SkillHashError",
    "SkillIntegrityError",
    "SkillLimitError",
    "SkillMatcher",
    "SkillNotFound",
    "SkillOrderError",
    "SkillRisk",
    "SkillSlotError",
    "SkillState",
    "SkillSyntaxError",
    "SkillTrust",
    "SkillUnknownError",
    "SkillVersionError",
    "SkillsRegistry",
    "Slot",
    "SlotType",
    "Trust",
    "Verdict",
    "body_digest",
    "canonicalize",
    "capture_from_flow",
    "card_body_path",
    "check_skill_transition",
    "evaluate_card_rules",
    "footer_line",
    "is_sha256_hex",
    "match_skills",
    "parse_card",
    "read_body_safely",
    "render_card",
    "trajectory_trust",
    "verify_footer",
    "write_body_atomically",
]

