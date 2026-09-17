"""Unit tests for skill lifecycle state machine and transitions (P2.1.g)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.skills.format import render_card
from antigona.skills.lifecycle import InvalidTransition, SkillState, check_skill_transition
from antigona.skills.records import Origin, PlanStep, RiskCeiling, SkillCard, Trust, Verdict
from antigona.skills.registry import SkillsRegistry


def make_valid_card() -> bytes:
    card = SkillCard(
        skill_id="skl-lifecycle-test-0001",
        slug="lifecycle-test",
        version=1,
        owner_id="owner-42",
        trust=Trust.TRUSTED,
        risk=RiskCeiling.LOW,
        intent=("Test lifecycle",),
        plan=(PlanStep(number=1, tool="workspace.mkdir", args=(("path", "test"),)),),
        origin=Origin(
            flow="flow-123",
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


def test_illegal_transition_raises_and_is_journaled(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card_body = make_valid_card()
    skill = registry.register_skill(card_body, owner_id="owner-42", state_root=str(tmp_path))

    with pytest.raises(InvalidTransition, match="requires Verifier"):
        registry.transition_to(skill, SkillState.ACTIVE.value, actor="worker")

    with pytest.raises(InvalidTransition):
        check_skill_transition(SkillState.DRAFT, SkillState.ACTIVE)


def test_double_promote_cas_second_loses(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card_body = make_valid_card()
    skill = registry.register_skill(card_body, owner_id="owner-42", state_root=str(tmp_path))

    candidate = registry.transition_to(skill, SkillState.CANDIDATE.value, actor="worker")
    assert candidate.status == SkillState.CANDIDATE.value

    promoted = registry.promote(candidate.id, verifier_actor="verifier-1")
    assert promoted.status == SkillState.ACTIVE.value

    with pytest.raises(InvalidTransition, match="promote requires CANDIDATE status"):
        registry.promote(candidate.id, verifier_actor="verifier-2")


def test_quarantine_is_sticky(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card_body = make_valid_card()
    skill = registry.register_skill(card_body, owner_id="owner-42", state_root=str(tmp_path))

    quarantined = registry.quarantine(skill, actor="verifier", reason="poisoned")
    assert quarantined.status == SkillState.QUARANTINED.value

    with pytest.raises(InvalidTransition):
        registry.transition_to(quarantined, SkillState.DRAFT.value, actor="admin")


def test_deprecate_active_ok(tmp_path: Path, db_session: Session) -> None:
    registry = SkillsRegistry(db_session)
    card_body = make_valid_card()
    skill = registry.register_skill(card_body, owner_id="owner-42", state_root=str(tmp_path))
    candidate = registry.transition_to(skill, SkillState.CANDIDATE.value, actor="worker")
    active = registry.promote(candidate.id, verifier_actor="verifier")

    deprecated = registry.deprecate(active.id, actor="owner-42")
    assert deprecated.status == SkillState.DEPRECATED.value
