"""Verifier-side skill validation checks before promotion to ACTIVE.

Independent verifier checks run out-of-band to ensure candidate skill cards:
1. match body_sha256 digest and parse cleanly;
2. use only tools registered in Worker's tool registry;
3. satisfy trust-degradation and owner risk policy limits;
4. originate from a verified DONE task flow;
5. contain no verification criteria leakage or secret-like values.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artifact, TaskFlow, TaskState
from ..skills.canonical import verify_footer
from ..skills.errors import SkillFormatError
from ..skills.format import parse_card
from ..skills.records import SkillCard, SkillRisk, SkillTrust

__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "SkillVerificationError",
    "verify_skill_card_for_promotion",
]


class SkillVerificationError(Exception):
    """Raised when verifier skill promotion checks fail."""


RISK_ORDER: dict[SkillRisk, int] = {
    SkillRisk.LOW: 1,
    SkillRisk.MEDIUM: 2,
    SkillRisk.HIGH: 3,
}

POLICY_ORDER: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
}

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"bearer\s+[a-zA-Z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"eyJ[a-zA-Z0-9_-]{20,}"),
]

DEFAULT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "workspace.write_text",
        "workspace.read_text",
        "workspace.mkdir",
        "sandbox.shell",
        "web.fetch",
    }
)


def verify_skill_card_for_promotion(
    session: Session,
    card_body: bytes,
    expected_sha256: str,
    source_flow_id: str | None,
    *,
    tool_registry: Sequence[str] | None = None,
    owner_risk_policy: str = "HIGH",
) -> SkillCard:
    """Perform verifier checks on a candidate skill card before promotion.

    Raises :class:`SkillVerificationError` if any verification rule fails.
    Returns the parsed :class:`SkillCard` on success.
    """
    # 1. Digest & Parse Check
    try:
        digest, _ = verify_footer(card_body)
    except Exception as exc:
        raise SkillVerificationError(f"footer integrity check failed: {exc}") from exc

    if digest != expected_sha256:
        raise SkillVerificationError(
            f"body sha256 mismatch: expected {expected_sha256}, got {digest}"
        )

    try:
        card = parse_card(card_body)
    except SkillFormatError as exc:
        raise SkillVerificationError(f"card format invalid: {exc}") from exc

    # 2. Tool Registry Check
    allowed_tools = (
        frozenset(tool_registry) if tool_registry is not None else DEFAULT_ALLOWED_TOOLS
    )
    for step in card.plan:
        if step.tool not in allowed_tools:
            raise SkillVerificationError(
                f"plan step tool '{step.tool}' is not in worker tool registry"
            )

    # 3. Trust & Risk Ceiling Check
    if card.trust == SkillTrust.UNTRUSTED and card.risk != SkillRisk.LOW:
        raise SkillVerificationError(
            f"untrusted skill must have LOW risk ceiling, got {card.risk.value}"
        )

    card_risk_level = RISK_ORDER.get(card.risk, 3)
    policy_level = POLICY_ORDER.get(owner_risk_policy.upper(), 3)
    if card_risk_level > policy_level:
        raise SkillVerificationError(
            f"skill risk ceiling {card.risk.value} exceeds owner policy {owner_risk_policy}"
        )

    # 4. Source TaskFlow & Artifact Verification
    if source_flow_id:
        task = session.scalar(select(TaskFlow).where(TaskFlow.id == source_flow_id))
        if not task:
            raise SkillVerificationError(f"source task flow {source_flow_id} not found")
        if task.status != TaskState.DONE.value:
            raise SkillVerificationError(
                f"source task flow {source_flow_id} status is {task.status}, expected DONE"
            )
        artifacts = session.scalars(
            select(Artifact).where(Artifact.task_id == source_flow_id)
        ).all()
        if not any(art.verified for art in artifacts):
            raise SkillVerificationError(
                f"source task flow {source_flow_id} has no verified artifacts"
            )

    # 5. Check for secrets leakage
    raw_text = card_body.decode("utf-8", errors="replace")
    for pattern in SECRET_PATTERNS:
        if pattern.search(raw_text):
            raise SkillVerificationError("card body contains secret-like values")

    return card
