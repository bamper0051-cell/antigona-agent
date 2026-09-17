"""Deterministic skill matcher.

Implemented in P2.1.i: owner-isolated, ``ACTIVE``-only candidate selection scored by the
number of ``[match]`` rules that fire under ``mode=all``/``mode=any``, with the stable
tie-break ``(-score, -version, id)``. No LLM, no network, no ``[intent]`` prose.

Fail-closed on card bodies: a registered body that cannot be read or parsed scores 0.
Legacy ``trigger`` matching is used only for rows that have no card body.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Skill
from .errors import SkillFormatError, SkillIntegrityError
from .lifecycle import SkillState
from .records import MatchKind, MatchMode, MatchRule
from .store import CardStore

__all__ = [
    "MatchResult",
    "SkillMatcher",
    "evaluate_card_rules",
    "match_skills",
]

logger = logging.getLogger(__name__)

MAX_KW_PREFIX_CHARS = 4

# Sort key: (-score, -version, id, slug, owner_id) — last two ride along for results.
_ScoredRow = tuple[int, int, str, str, str]


class MatchResult:
    """One matched skill candidate with its deterministic score."""

    __slots__ = ("skill_id", "slug", "version", "score", "owner_id")

    def __init__(
        self,
        skill_id: str,
        slug: str,
        version: int,
        score: int,
        owner_id: str,
    ) -> None:
        self.skill_id = skill_id
        self.slug = slug
        self.version = version
        self.score = score
        self.owner_id = owner_id

    def __repr__(self) -> str:
        return (
            f"MatchResult(skill_id={self.skill_id!r}, slug={self.slug!r}, "
            f"version={self.version}, score={self.score})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MatchResult):
            return NotImplemented
        return (
            self.skill_id == other.skill_id
            and self.slug == other.slug
            and self.version == other.version
            and self.score == other.score
        )

    def __hash__(self) -> int:
        return hash((self.skill_id, self.slug, self.version, self.score))


def evaluate_card_rules(
    rules: tuple[MatchRule, ...],
    match_mode: MatchMode | None,
    goal: str,
    available_tools: set[str],
) -> int:
    """Evaluate match rules from a parsed SkillCard and return the score.

    Returns the number of triggered rules (0 means no match). When ``mode=all``,
    all rules must fire; ``mode=any`` requires at least one.
    """
    if not rules:
        return 0
    mode = match_mode or MatchMode.ALL
    fired = sum(1 for r in rules if _evaluate_single_rule(r, goal, available_tools))
    if mode is MatchMode.ALL and fired < len(rules):
        return 0
    if mode is MatchMode.ANY and fired == 0:
        return 0
    return fired


def _evaluate_single_rule(rule: MatchRule, goal: str, tools: set[str]) -> bool:
    """Evaluate one :class:`MatchRule` against goal and available tools."""
    if rule.kind is MatchKind.KEYWORD:
        return _check_kw(rule.values, goal)
    if rule.kind is MatchKind.PATH_PREFIX:
        return _check_path_prefix(rule.values, goal)
    if rule.kind is MatchKind.TOOL_AVAILABLE:
        return _check_tool_available(rule.values, tools)
    return False


def _check_kw(values: tuple[str, ...], goal: str) -> bool:
    """Return ``True`` if any keyword value matches the goal text.

    Short values (≤4 chars) match as substring. Longer values match on word
    boundaries. Comparisons are case-insensitive.
    """
    goal_lower = goal.lower()
    for value in values:
        val_lower = value.lower()
        if len(val_lower) <= MAX_KW_PREFIX_CHARS:
            if val_lower in goal_lower:
                return True
        else:
            idx = goal_lower.find(val_lower)
            while idx != -1:
                start_ok = idx == 0 or not goal_lower[idx - 1].isalnum()
                end = idx + len(val_lower)
                end_ok = end >= len(goal_lower) or not goal_lower[end].isalnum()
                if start_ok and end_ok:
                    return True
                idx = goal_lower.find(val_lower, idx + 1)
    return False


def _check_path_prefix(values: tuple[str, ...], goal: str) -> bool:
    """Return ``True`` if the goal starts with any path-prefix value."""
    for value in values:
        if goal.startswith(value):
            return True
    return False


def _check_tool_available(values: tuple[str, ...], tools: set[str]) -> bool:
    """Return ``True`` if any required tool is available.

    When no tool information is provided (empty set), all tool-available
    rules trivially pass — the matcher cannot filter on what it doesn't know.
    """
    if not tools:
        return True
    for value in values:
        if value in tools:
            return True
    return False


def _match_by_trigger(trigger: str, goal: str) -> int:
    """P0 backward-compatible trigger matching for rows without a card body.

    Splits the trigger on ``|`` or ``,`` and counts how many tokens appear
    (case-insensitively) in the goal. Never used as a fallback when a card body
    is registered but unreadable or unparsable.
    """
    if not trigger:
        return 0
    score = 0
    for token in trigger.replace("|", ",").split(","):
        token = token.strip()
        if token and token.lower() in goal.lower():
            score += 1
    return score


def _has_card_body(skill: Skill) -> bool:
    """True as soon as a body digest is registered.

    ``body_bytes`` is only a denormalized byte counter, so partial metadata
    (digest present, counter still 0) must **not** demote the row to legacy
    trigger matching — a registered body always goes through the fail-closed path.
    """
    return bool(skill.body_sha256)


def _score_skill(
    skill: Skill,
    goal: str,
    tools: set[str],
    store: CardStore | None,
) -> int:
    """Compute the match score for a single skill row.

    * Card body present + store → evaluate ``[match]`` rules only.
    * Card body present but unreadable/unparsable → **0** (fail-closed).
    * Card body present but no store → **0** (cannot verify body).
    * No card body → legacy denormalized ``trigger`` field.
    """
    if _has_card_body(skill):
        if store is None:
            logger.warning(
                "skill %s has a card body but no store; refusing match",
                skill.id,
            )
            return 0
        try:
            body = store.read(skill.body_sha256)
            from .format import parse_card  # noqa: PLC0415

            card = parse_card(body)
            return evaluate_card_rules(card.match, card.match_mode, goal, tools)
        except (SkillIntegrityError, SkillFormatError, OSError) as exc:
            logger.warning(
                "skill %s card body failed closed (%s); score=0",
                skill.id,
                exc,
            )
            return 0

    return _match_by_trigger(skill.trigger, goal)


def _score_rows(
    skills: Iterable[Skill],
    goal: str,
    tools: set[str],
    store: CardStore | None,
) -> list[_ScoredRow]:
    """Score skills and return sort keys with slug/owner for result build."""
    scored: list[_ScoredRow] = []
    for skill in skills:
        score = _score_skill(skill, goal, tools, store)
        if score == 0:
            continue
        # Stable tie-break: (-score, -version, id); slug/owner ride along.
        scored.append((-score, -skill.version, skill.id, skill.slug, skill.owner_id or ""))
    scored.sort()
    return scored


def _results_from_scored(scored: Sequence[_ScoredRow]) -> list[MatchResult]:
    return [
        MatchResult(
            skill_id=skill_id,
            slug=slug,
            version=-neg_version,
            score=-neg_score,
            owner_id=owner_id,
        )
        for neg_score, neg_version, skill_id, slug, owner_id in scored
    ]


class SkillMatcher:
    """Deterministic matcher that queries the DB and evaluates structural rules.

    Usage::

        matcher = SkillMatcher(session)
        results = matcher.match(
            owner_id="owner-42",
            goal="собери отчёт в reports/",
            available_tools={"workspace.write_text", "workspace.mkdir"},
        )
    """

    def __init__(
        self,
        session: Session,
        store: CardStore | None = None,
    ) -> None:
        self._session = session
        self._store = store

    def match(
        self,
        owner_id: str,
        goal: str,
        available_tools: set[str] | None = None,
    ) -> list[MatchResult]:
        """Find and score matching ACTIVE skills for the given owner and goal."""
        tools = available_tools or set()
        skills: Sequence[Skill] = self._session.scalars(
            select(Skill).where(
                Skill.owner_id == owner_id,
                Skill.status == SkillState.ACTIVE.value,
            )
        ).all()
        return _results_from_scored(_score_rows(skills, goal, tools, self._store))

    def match_from_skills(
        self,
        skills: Iterable[Skill],
        goal: str,
        available_tools: set[str] | None = None,
    ) -> list[MatchResult]:
        """Score a pre-filtered iterable of skills (for testing, no DB query)."""
        tools = available_tools or set()
        return _results_from_scored(_score_rows(skills, goal, tools, self._store))


def match_skills(
    session: Session,
    owner_id: str,
    goal: str,
    state_root: str | None = None,
    available_tools: set[str] | None = None,
) -> list[MatchResult]:
    """Top-level helper to match ACTIVE skills for an owner and goal."""
    store = CardStore(state_root) if state_root else None
    matcher = SkillMatcher(session, store=store)
    return matcher.match(owner_id=owner_id, goal=goal, available_tools=available_tools)

