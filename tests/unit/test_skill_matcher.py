"""Unit tests for deterministic skill matcher (P2.1.i)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.skills.format import render_card
from antigona.skills.lifecycle import SkillState
from antigona.skills.matcher import SkillMatcher, match_skills
from antigona.skills.records import (
    MatchKind,
    MatchMode,
    MatchRule,
    Origin,
    PlanStep,
    RiskCeiling,
    SkillCard,
    Trust,
    Verdict,
)
from antigona.skills.registry import SkillsRegistry
from antigona.skills.store import CardStore


def make_card(
    skill_id: str,
    slug: str,
    version: int,
    owner: str,
) -> bytes:
    card = SkillCard(
        skill_id=skill_id,
        slug=slug,
        version=version,
        owner_id=owner,
        trust=Trust.TRUSTED,
        risk=RiskCeiling.LOW,
        intent=("Generates report",),
        match_mode=MatchMode.ALL,
        match=(
            MatchRule(kind=MatchKind.KEYWORD, values=("report", "summary")),
            MatchRule(kind=MatchKind.TOOL_AVAILABLE, values=("workspace.write_text",)),
        ),
        plan=(PlanStep(number=1, tool="workspace.mkdir", args=(("path", "reports"),)),),
        origin=Origin(
            flow="flow-1",
            steps=1,
            captured=datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC),
            verdict=Verdict.VERIFIER_PASS,
            trust_at_capture=Trust.TRUSTED,
        ),
    )
    return render_card(card)


@pytest.fixture
def db_session(tmp_path: Path) -> Session:
    db = Database(f"sqlite:///{tmp_path}/test.db")
    db.create_all()
    with db.session_factory() as session:
        yield session


def test_matcher_is_deterministic_across_runs(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card1 = make_card("skl-match-0001", "report-generator", 1, "owner-1")
    card2 = make_card("skl-match-0002", "report-generator-v2", 2, "owner-1")

    skill1 = registry.register_skill(card1, owner_id="owner-1", state_root=str(tmp_path))
    skill2 = registry.register_skill(card2, owner_id="owner-1", state_root=str(tmp_path))

    c1 = registry.transition_to(skill1, SkillState.CANDIDATE.value, actor="w")
    c2 = registry.transition_to(skill2, SkillState.CANDIDATE.value, actor="w")
    registry.promote(c1.id, verifier_actor="v")
    registry.promote(c2.id, verifier_actor="v")

    matches_1 = match_skills(
        db_session, owner_id="owner-1", goal="generate report summary", state_root=str(tmp_path)
    )
    matches_2 = match_skills(
        db_session, owner_id="owner-1", goal="generate report summary", state_root=str(tmp_path)
    )

    assert [m.skill_id for m in matches_1] == [m.skill_id for m in matches_2]


def test_owner_isolation(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card1 = make_card("skl-match-0001", "report-generator", 1, "owner-1")
    skill1 = registry.register_skill(card1, owner_id="owner-1", state_root=str(tmp_path))
    c1 = registry.transition_to(skill1, SkillState.CANDIDATE.value, actor="w")
    registry.promote(c1.id, verifier_actor="v")

    matches = match_skills(
        db_session, owner_id="owner-2", goal="generate report summary", state_root=str(tmp_path)
    )
    assert len(matches) == 0


def test_only_active_skills_match(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card1 = make_card("skl-match-0001", "report-generator", 1, "owner-1")
    registry.register_skill(card1, owner_id="owner-1", state_root=str(tmp_path))

    matches = match_skills(
        db_session, owner_id="owner-1", goal="generate report summary", state_root=str(tmp_path)
    )
    assert len(matches) == 0


def test_stable_tiebreak_order(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card1 = make_card("skl-match-0001", "report-generator", 1, "owner-1")
    card2 = make_card("skl-match-0002", "report-generator-v2", 2, "owner-1")

    s1 = registry.register_skill(card1, owner_id="owner-1", state_root=str(tmp_path))
    s2 = registry.register_skill(card2, owner_id="owner-1", state_root=str(tmp_path))

    c1 = registry.transition_to(s1, SkillState.CANDIDATE.value, actor="w")
    c2 = registry.transition_to(s2, SkillState.CANDIDATE.value, actor="w")
    registry.promote(c1.id, verifier_actor="v")
    registry.promote(c2.id, verifier_actor="v")

    matches = match_skills(
        db_session, owner_id="owner-1", goal="report summary", state_root=str(tmp_path)
    )
    assert len(matches) == 2
    assert matches[0].version >= matches[1].version


def test_corrupt_card_body_fail_closed_no_trigger_fallback(
    tmp_path: Path, db_session: Session
) -> None:
    """Registered body that fails integrity must score 0 — never fall back to trigger."""
    registry = SkillsRegistry(db_session)
    card = make_card("skl-match-bad1", "report-generator", 1, "owner-1")
    skill = registry.register_skill(card, owner_id="owner-1", state_root=str(tmp_path))
    # Ensure legacy trigger would have matched if used as a bandaid.
    skill.trigger = "report,summary"
    db_session.commit()

    candidate = registry.transition_to(skill, SkillState.CANDIDATE.value, actor="w")
    registry.promote(candidate.id, verifier_actor="v")

    # Tamper with the content-addressed body on disk.
    body_path = CardStore(tmp_path).path(skill.body_sha256)
    body_path.write_bytes(b"tampered-not-a-valid-card\n")

    matches = match_skills(
        db_session,
        owner_id="owner-1",
        goal="generate report summary",
        state_root=str(tmp_path),
    )
    assert matches == []


def test_card_body_without_store_fail_closed(db_session: Session) -> None:
    """Body present but no store → refuse match (cannot verify card)."""
    # Minimal ACTIVE row with body metadata but no readable store.
    from antigona.models import Skill

    skill = Skill(
        id="skl-no-store-0001",
        name="orphan-body",
        trigger="report,summary",
        trajectory_ref="flow-x",
        owner_id="owner-1",
        slug="orphan-body",
        version=1,
        status=SkillState.ACTIVE.value,
        body_sha256="a" * 64,
        body_bytes=100,
    )
    db_session.add(skill)
    db_session.commit()

    matches = match_skills(
        db_session,
        owner_id="owner-1",
        goal="generate report summary",
        state_root=None,
    )
    assert matches == []


def test_partial_body_metadata_fail_closed_no_trigger_fallback(db_session: Session) -> None:
    """Digest registered but body_bytes still 0 → fail-closed, never legacy trigger."""
    from antigona.models import Skill

    skill = Skill(
        id="skl-partial-meta-1",
        name="partial-body",
        trigger="report,summary",
        trajectory_ref="flow-z",
        owner_id="owner-1",
        slug="partial-body",
        version=1,
        status=SkillState.ACTIVE.value,
        body_sha256="a" * 64,
        body_bytes=0,
    )
    db_session.add(skill)
    db_session.commit()

    matches = match_skills(
        db_session,
        owner_id="owner-1",
        goal="generate report summary",
        state_root=None,
    )
    assert matches == []


def test_legacy_trigger_only_when_no_card_body(db_session: Session) -> None:
    """P0 rows without a card body still match via denormalized trigger."""
    from antigona.models import Skill

    skill = Skill(
        id="skl-legacy-0001",
        name="legacy",
        trigger="report,summary",
        trajectory_ref="flow-y",
        owner_id="owner-1",
        slug="legacy",
        version=1,
        status=SkillState.ACTIVE.value,
        body_sha256="",
        body_bytes=0,
    )
    db_session.add(skill)
    db_session.commit()

    matches = match_skills(
        db_session,
        owner_id="owner-1",
        goal="generate report summary",
        state_root=None,
    )
    assert len(matches) == 1
    assert matches[0].skill_id == "skl-legacy-0001"
    assert matches[0].score == 2


def test_match_from_skills_preserves_per_skill_owner_id(db_session: Session) -> None:
    """Each MatchResult must carry its own skill.owner_id (not the last loop item)."""
    skills = [
        SimpleNamespace(
            id="skl-a",
            slug="alpha",
            version=1,
            owner_id="owner-a",
            trigger="alpha",
            body_sha256="",
            body_bytes=0,
        ),
        SimpleNamespace(
            id="skl-b",
            slug="beta",
            version=1,
            owner_id="owner-b",
            trigger="beta",
            body_sha256="",
            body_bytes=0,
        ),
    ]
    matcher = SkillMatcher(db_session, store=None)
    results = matcher.match_from_skills(skills, goal="alpha and beta tokens")  # type: ignore[arg-type]
    by_id = {r.skill_id: r for r in results}
    assert by_id["skl-a"].owner_id == "owner-a"
    assert by_id["skl-b"].owner_id == "owner-b"
