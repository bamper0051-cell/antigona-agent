"""Capture of a ``DRAFT`` card from a finished trajectory.

Implemented in P2.1.j: reads ``flow_steps``/``state_transitions``/``artifacts`` of a
``DONE`` flow, redacts secret-like values through the observability redactor, and
inherits the P1.3 trust label — an untrusted trajectory yields ``%trust untrusted`` with
a ``LOW`` risk ceiling. The result is always ``DRAFT``; there is no auto-promotion.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .registry import SkillsRegistry


from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artifact, FlowStep, StepState, TaskFlow, TaskState
from ..observability import redact
from .errors import SkillNotFound
from .format import render_card
from .records import (
    Claim,
    ClaimKind,
    Origin,
    PlanStep,
    RiskCeiling,
    SkillCard,
    Trust,
    Verdict,
)
from .store import CardStore

__all__ = [
    "CaptureEngine",
    "build_card_from_trajectory",
    "capture_from_flow",
    "redact_plan_step_args",
    "trajectory_trust",
]

#: Highest risk ceiling for an untrusted capture.
_UNTRUSTED_CEILING = RiskCeiling.LOW
#: Default risk ceiling for a trusted capture.
_DEFAULT_CEILING = RiskCeiling.MEDIUM
#: Substrings in a step's tool name that mark a network call.
_NETWORK_TOOL_MARKERS = ("fetch", "http", "web")


def _make_slug(name: str, flow_id: str) -> str:
    """Derive a deterministic slug from the flow's goal or id."""
    import re

    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48]
    return slug or f"capture-{flow_id[:12]}"


def redact_plan_step_args(
    args: dict[str, Any],
    *,
    untrusted: bool = False,
) -> dict[str, Any]:
    """Redact secret-like values from plan step arguments.

    Uses :func:`antigona.observability.redact` to replace credentials and
    sensitive data with ``[REDACTED]`` markers.

    When ``untrusted=True``, also redacts any URL-like strings
    (``https?://``) because the format spec forbids URLs in plans of
    untrusted cards.
    """
    import re as _re

    result: dict[str, Any] = {}
    for key, value in args.items():
        result[key] = redact(value)
    # Redact API-key-like patterns in string values (sk-/pk-/api-/key- prefixes)
    _API_KEY_RX = _re.compile(r"(?i)\b(sk-|pk-|api-|key-)[a-z0-9]{16,}\b")
    _URL_RX = (
        _re.compile(r"https?://[^\s\",;\]\}]+")
        if untrusted
        else None
    )
    for key, value in result.items():
        if isinstance(value, str):
            if _API_KEY_RX.search(value):
                result[key] = _API_KEY_RX.sub("[REDACTED]", value)
            if untrusted and _URL_RX and _URL_RX.search(value):
                result[key] = _URL_RX.sub("[REDACTED]", value)
    return result


def _is_untrusted_label(value: object) -> bool:
    """True only for the structured label ``\"untrusted\"`` (case-insensitive)."""
    return isinstance(value, str) and value.strip().lower() == Trust.UNTRUSTED.value


def _tool_args_untrusted(task: TaskFlow) -> bool:
    """Explicit trust flags on ``task.tool_arguments`` (P1.3 structured home)."""
    args = task.tool_arguments
    if not isinstance(args, dict):
        return False
    if args.get("untrusted") is True:
        return True
    return _is_untrusted_label(args.get("trust"))


def _artifact_evidence_untrusted(evidence: object) -> bool:
    """Explicit trust flags on ``Artifact.evidence`` JSON — no free-text search."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("untrusted") is True:
        return True
    return _is_untrusted_label(evidence.get("trust"))


def trajectory_trust(session: Session, task: TaskFlow) -> Trust:
    """Resolve trajectory trust from structured fields only (P1.3).

    Order (first hit wins as untrusted):

    1. ``task.tool_arguments`` — ``untrusted: true`` or ``trust: \"untrusted\"``
    2. Any artifact ``evidence`` with the same structured keys

    Free-text greps of ``goal`` / ``content`` / transition ``reason`` / ``actor``
    are intentionally **not** signals: the word \"untrusted\" in prose must not
    poison capture.
    """
    if _tool_args_untrusted(task):
        return Trust.UNTRUSTED

    artifacts = session.scalars(
        select(Artifact).where(Artifact.task_id == task.id)
    ).all()
    for artifact in artifacts:
        if _artifact_evidence_untrusted(artifact.evidence):
            return Trust.UNTRUSTED

    return Trust.TRUSTED


def _has_untrusted_trajectory(session: Session, task: TaskFlow) -> bool:
    """Back-compat wrapper: ``True`` when :func:`trajectory_trust` is untrusted."""
    return trajectory_trust(session, task) is Trust.UNTRUSTED


def _step_tool_name(step: FlowStep) -> str:
    """Resolve the tool a step actually ran.

    ``FlowStep.tool_name`` is the structured home; ``input["tool_name"]`` and the
    step title are only fallbacks for rows that predate it.
    """
    return str(step.tool_name or step.input.get("tool_name", step.title))


def _build_plan_from_steps(
    steps: Sequence[FlowStep],
    *,
    untrusted: bool = False,
) -> list[PlanStep]:
    """Build ``[plan]`` entries from executed flow steps.

    Each step becomes one numbered plan entry with the tool name and its
    input arguments (redacted).

    When ``untrusted=True``, URLs in arguments are also redacted.
    """
    plan: list[PlanStep] = []
    for step in steps:
        if step.status != StepState.COMPLETED.value:
            continue
        tool_name = _step_tool_name(step)
        args = step.arguments or step.input.get("arguments", step.input)
        redacted = redact_plan_step_args(dict(args), untrusted=untrusted)
        plan.append(
            PlanStep(
                number=step.step_number or step.index,
                tool=str(tool_name),
                args=tuple(sorted(redacted.items())),
            )
        )
    return plan


def _aggregate_claims(
    steps: Sequence[FlowStep],
    artifacts: Sequence[Artifact],
) -> list[Claim]:
    """Aggregate ``[claims]`` from the executed trajectory.

    Claims are derived from artifacts produced by the flow: each artifact
    with a ``verified`` flag contributes a ``produces-file`` claim.
    """
    claims: list[Claim] = []
    seen_paths: set[str] = set()
    for artifact in artifacts:
        if artifact.path and artifact.path not in seen_paths:
            seen_paths.add(artifact.path)
            claims.append(Claim(kind=ClaimKind.PRODUCES_FILE, value=artifact.path))
            if artifact.verified:
                claims.append(Claim(kind=ClaimKind.PRODUCES_NONEMPTY, value=True))
    # Deduplicate claims.
    seen_kinds: set[str] = set()
    unique: list[Claim] = []
    for claim in claims:
        key = f"{claim.kind.value}:{claim.value}"
        if key not in seen_kinds:
            seen_kinds.add(key)
            unique.append(claim)
    # Add no-network claim if no web fetch was involved. The tool is resolved from
    # FlowStep.tool_name (same as the plan) — input["tool_name"] alone would miss
    # network steps and yield a false no-network claim.
    has_network = any(
        marker in _step_tool_name(s).lower()
        for s in steps
        for marker in _NETWORK_TOOL_MARKERS
    )
    if not has_network:
        unique.append(Claim(kind=ClaimKind.NO_NETWORK, value=True))
    return unique


def build_card_from_trajectory(
    session: Session,
    task: TaskFlow,
    owner_id: str | None = None,
) -> SkillCard:
    """Construct a :class:`SkillCard` from a **DONE** flow's execution trace.

    The card is always in the ``DRAFT`` state with a new id. Trust is inherited
    from the trajectory: an untrusted trace produces ``%trust untrusted`` and
    ``%risk LOW``.
    """
    if task.status != TaskState.DONE.value:
        raise ValueError(
            f"cannot capture from non-DONE flow (status={task.status})"
        )

    steps = session.scalars(
        select(FlowStep)
        .where(FlowStep.task_id == task.id)
        .order_by(FlowStep.index)
    ).all()

    artifacts = session.scalars(
        select(Artifact).where(Artifact.task_id == task.id)
    ).all()

    owner = owner_id or task.owner_id or "unknown"
    trust = trajectory_trust(session, task)
    untrusted = trust is Trust.UNTRUSTED
    risk = _UNTRUSTED_CEILING if untrusted else _DEFAULT_CEILING

    # Redact API-key-like patterns from the goal before slug generation
    import re as _re
    _goal_raw = task.goal
    _goal_raw = _re.sub(r"(?i)\b(sk-|pk-|api-|key-)[a-z0-9]{16,}\b", "[REDACTED]", _goal_raw)
    slug = _make_slug(str(redact(_goal_raw)), task.id)
    captured_at = datetime.now(UTC)
    plan_steps = _build_plan_from_steps(steps, untrusted=untrusted)
    claims = _aggregate_claims(steps, artifacts)

    card = SkillCard(
        skill_id="skl-" + str(uuid.uuid4()),
        slug=slug,
        version=1,
        owner_id=owner,
        trust=trust,
        risk=risk,
        intent=(str(redact(_goal_raw)),),
        plan=tuple(plan_steps),
        origin=Origin(
            flow=task.id,
            steps=len(steps),
            captured=captured_at,
            verdict=Verdict.VERIFIER_PASS,
            trust_at_capture=trust,
        ),
        format_version=1,
        claims=tuple(claims),
    )
    return card


def capture_from_flow(
    session: Session,
    task_id: str,
    store: CardStore | str | Path | None = None,
    owner_id: str | None = None,
    *,
    state_root: str | Path | None = None,
    registry: SkillsRegistry | None = None,
) -> Any:
    """Capture a DONE flow's trajectory as a DRAFT skill card.

    The card body is written to the ``CardStore`` (content-addressed).
    If a ``registry`` is provided, the skill is registered in the DB and the
    :class:`Skill` row is returned. Otherwise the :class:`SkillCard` is returned.

    Raises ``ValueError`` if the flow is not in DONE status.
    Raises ``SkillNotFound`` if the flow does not exist.
    """
    from .registry import SkillsRegistry

    task = session.scalar(select(TaskFlow).where(TaskFlow.id == task_id))
    if not task:
        raise SkillNotFound(f"flow {task_id} not found")

    if task.status != TaskState.DONE.value:
        raise ValueError(f"flow {task_id} is not in DONE status: got {task.status}")

    card = build_card_from_trajectory(session, task, owner_id=owner_id)
    body = render_card(card)

    root = state_root or (store.root if isinstance(store, CardStore) else store) or "./state"
    card_store = store if isinstance(store, CardStore) else CardStore(root)
    card_store.write(body)

    if registry is not None or state_root is not None:
        reg = registry or SkillsRegistry(session)
        return reg.register_skill(
            card_body=body,
            owner_id=card.owner_id,
            state_root=str(card_store.root),
            actor="capture",
        )

    return card



class CaptureEngine:
    """Capture engine: reads a DONE flow's trajectory and produces a DRAFT card.

    The card is always stored in the content-addressed ``CardStore`` before
    being returned.
    """

    def __init__(
        self,
        session: Session,
        state_root: str | Path,
    ) -> None:
        self._session = session
        self._store = CardStore(state_root)

    def capture(self, task_id: str, owner_id: str | None = None) -> SkillCard:
        """Capture a DONE flow as a DRAFT skill card.

        Returns the parsed :class:`SkillCard` with body already stored.
        """
        return cast(
            SkillCard,
            capture_from_flow(
                self._session,
                task_id,
                self._store,
                owner_id=owner_id,
            ),
        )

