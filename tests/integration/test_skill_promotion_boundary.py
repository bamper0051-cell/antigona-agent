"""Integration tests for skill promotion security boundary (P2.1.h)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.skills.format import render_card
from antigona.skills.lifecycle import SkillState
from antigona.skills.records import Origin, PlanStep, RiskCeiling, SkillCard, Trust, Verdict
from antigona.skills.registry import SkillsRegistry
from antigona.verifier_service import create_verifier_app


def make_valid_card(skill_id: str = "skl-promo-0001") -> bytes:
    card = SkillCard(
        skill_id=skill_id,
        slug="promo-test",
        version=1,
        owner_id="owner-42",
        trust=Trust.TRUSTED,
        risk=RiskCeiling.LOW,
        intent=("Promo test card",),
        plan=(PlanStep(number=1, tool="workspace.mkdir", args=(("path", "test"),)),),
        origin=Origin(
            flow="flow-done-1",
            steps=1,
            captured=datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC),
            verdict=Verdict.VERIFIER_PASS,
            trust_at_capture=Trust.TRUSTED,
        ),
    )
    return render_card(card)


@pytest.fixture
def verifier_client(tmp_path: Path) -> tuple[TestClient, Database, Session]:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    db = Database(db_url)
    db.create_all()

    app = create_verifier_app(
        database_url=db_url,
        credential="test-verifier-token",
        judge=None,
    )
    client = TestClient(app)
    with db.session_factory() as session:
        yield client, db, session


def test_promote_without_bearer_returns_401(verifier_client: tuple[TestClient, Database, Session]) -> None:
    client, _, _ = verifier_client
    resp = client.post("/skills/skl-123/promote", json={"revision": 0, "correlation_id": "test-cid"})
    assert resp.status_code == 401


def test_promote_with_wrong_revision_returns_conflict(tmp_path: Path, verifier_client: tuple[TestClient, Database, Session]) -> None:
    client, _, session = verifier_client
    registry = SkillsRegistry(session)
    skill = registry.register_skill(make_valid_card(), owner_id="owner-42", state_root=str(tmp_path))
    candidate = registry.transition_to(skill, SkillState.CANDIDATE.value, actor="worker")

    headers = {"Authorization": "Bearer test-verifier-token"}
    resp = client.post(
        f"/skills/{candidate.id}/promote",
        json={"revision": 999, "correlation_id": "test-cid"},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "revision mismatch" in resp.json()["detail"]


def test_failed_claim_moves_to_rejected_not_active(tmp_path: Path, verifier_client: tuple[TestClient, Database, Session]) -> None:
    client, _, session = verifier_client
    registry = SkillsRegistry(session)

    # Card missing source flow in DB -> promotion check fails
    skill = registry.register_skill(make_valid_card(), owner_id="owner-42", state_root=str(tmp_path))
    candidate = registry.transition_to(skill, SkillState.CANDIDATE.value, actor="worker")

    headers = {"Authorization": "Bearer test-verifier-token"}
    resp = client.post(
        f"/skills/{candidate.id}/promote",
        json={"revision": candidate.revision, "correlation_id": "test-cid"},
        headers=headers,
    )
    assert resp.status_code == 409
    session.refresh(skill)
    assert skill.status == SkillState.REJECTED.value


def test_no_active_write_path_outside_verifier() -> None:
    """Static AST scan ensuring Gateway, Worker, and Repository do not mutate status='ACTIVE'."""
    targets = [
        Path("src/antigona/gateway"),
        Path("src/antigona/worker"),
        Path("src/antigona/repository.py"),
        Path("src/antigona/pipeline.py"),
    ]

    for target in targets:
        files = [target] if target.is_file() else list(target.glob("**/*.py"))
        for py_file in files:
            content = py_file.read_text(encoding="utf-8")
            assert "status='ACTIVE'" not in content, f"{py_file} contains forbidden status='ACTIVE'"
            assert 'status = "ACTIVE"' not in content, f"{py_file} contains forbidden status = \"ACTIVE\""
            assert 'status="ACTIVE"' not in content, f"{py_file} contains forbidden status=\"ACTIVE\""
            assert "SkillState.ACTIVE" not in content or "verifier" in py_file.name or "registry.py" in py_file.name, (
                f"{py_file} contains SkillState.ACTIVE assignment outside verifier/registry"
            )
