"""Integration tests for skill capture from task trajectories (P2.1.j)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.models import Artifact, FlowStep, StateTransition, StepState, TaskFlow, TaskState
from antigona.skills.capture import build_card_from_trajectory, capture_from_flow
from antigona.skills.lifecycle import SkillState
from antigona.skills.records import ClaimKind, SkillRisk, SkillTrust
from antigona.skills.registry import SkillsRegistry


@pytest.fixture
def db_session(tmp_path: Path) -> Session:
    db = Database(f"sqlite:///{tmp_path}/test.db")
    db.create_all()
    with db.session_factory() as session:
        yield session


def test_capture_from_done_flow_creates_draft(tmp_path: Path, db_session: Session) -> None:
    flow = TaskFlow(
        id="flow-done-1",
        owner_id="owner-42",
        goal="scaffold workspace report",
        status=TaskState.DONE.value,
        tool_name="workspace.mkdir",
        target_path="reports",
    )
    step1 = FlowStep(
        id="step-1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="workspace.mkdir",
        arguments={"path": "reports"},
    )
    artifact = Artifact(
        task_id=flow.id,
        step_id=step1.id,
        path="reports/index.md",
        sha256="abc123sha",
        size=100,
        verified=True,
    )
    db_session.add_all([flow, step1, artifact])
    db_session.commit()

    registry = SkillsRegistry(db_session)
    skill = capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)

    assert skill.status == SkillState.DRAFT.value
    assert skill.owner_id == "owner-42"
    assert skill.source_flow_id == flow.id


def test_capture_refuses_non_done_flow(tmp_path: Path, db_session: Session) -> None:
    flow = TaskFlow(
        id="flow-running-1",
        owner_id="owner-42",
        goal="running goal",
        status=TaskState.RUNNING.value,
        tool_name="workspace.mkdir",
        target_path="reports",
    )
    db_session.add(flow)
    db_session.commit()

    registry = SkillsRegistry(db_session)
    with pytest.raises(ValueError, match="is not in DONE status"):
        capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)


def test_untrusted_trajectory_yields_untrusted_card_with_low_ceiling(tmp_path: Path, db_session: Session) -> None:
    """Structured artifact evidence.untrusted=True → untrusted card + LOW ceiling."""
    flow = TaskFlow(
        id="flow-untrusted-1",
        owner_id="owner-42",
        goal="fetch external site",
        status=TaskState.DONE.value,
        tool_name="web.fetch",
        target_path="http://example.org",
    )
    step1 = FlowStep(
        id="step-u1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="web.fetch",
        arguments={"url": "http://example.org"},
    )
    artifact = Artifact(
        task_id=flow.id,
        step_id=step1.id,
        path="fetched.html",
        sha256="abc123sha",
        size=100,
        verified=True,
        evidence={"untrusted": True},
    )
    db_session.add_all([flow, step1, artifact])
    db_session.commit()

    registry = SkillsRegistry(db_session)
    skill = capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)

    assert skill.trust == SkillTrust.UNTRUSTED.value
    assert skill.risk_ceiling == SkillRisk.LOW.value


def test_tool_arguments_trust_label_marks_untrusted(tmp_path: Path, db_session: Session) -> None:
    """Explicit task.tool_arguments.trust=untrusted is a structured trust home."""
    flow = TaskFlow(
        id="flow-untrusted-args",
        owner_id="owner-42",
        goal="process payload",
        status=TaskState.DONE.value,
        tool_name="workspace.write_text",
        target_path="out.txt",
        tool_arguments={"trust": "untrusted"},
    )
    step1 = FlowStep(
        id="step-ua1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="workspace.write_text",
        arguments={"path": "out.txt", "content": "x"},
    )
    db_session.add_all([flow, step1])
    db_session.commit()

    registry = SkillsRegistry(db_session)
    skill = capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)

    assert skill.trust == SkillTrust.UNTRUSTED.value
    assert skill.risk_ceiling == SkillRisk.LOW.value


def test_prose_word_untrusted_does_not_poison_trust(tmp_path: Path, db_session: Session) -> None:
    """Word 'untrusted' in goal/reason must NOT mark capture untrusted without structured flags."""
    flow = TaskFlow(
        id="flow-prose-untrusted",
        owner_id="owner-42",
        goal="document how untrusted inputs are handled",
        content="note: never treat free-text untrusted as a signal",
        status=TaskState.DONE.value,
        tool_name="workspace.write_text",
        target_path="notes.md",
    )
    step1 = FlowStep(
        id="step-p1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="workspace.write_text",
        arguments={"path": "notes.md", "content": "ok"},
    )
    transition = StateTransition(
        task_id=flow.id,
        entity_id=flow.id,
        entity_type="task",
        from_state="PLANNING",
        to_state="RUNNING",
        reason="untrusted content fetched",  # free-text — must be ignored
        actor="untrusted-worker",
        correlation_id="cid-prose",
    )
    db_session.add_all([flow, step1, transition])
    db_session.commit()

    registry = SkillsRegistry(db_session)
    skill = capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)

    assert skill.trust == SkillTrust.TRUSTED.value
    assert skill.risk_ceiling == SkillRisk.MEDIUM.value


def test_network_tool_name_field_suppresses_no_network_claim(db_session: Session) -> None:
    """A network tool in FlowStep.tool_name must block the no-network claim."""
    flow = TaskFlow(
        id="flow-network-1",
        owner_id="owner-42",
        goal="download remote page",
        status=TaskState.DONE.value,
        tool_name="fetch_url",
        target_path="page.html",
    )
    step1 = FlowStep(
        id="step-n1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="fetch_url",  # structured home; input carries no tool_name
        arguments={"path": "page.html"},
    )
    db_session.add_all([flow, step1])
    db_session.commit()

    card = build_card_from_trajectory(db_session, flow)

    assert step1.input.get("tool_name") is None
    assert ClaimKind.NO_NETWORK not in {claim.kind for claim in card.claims}


def test_local_only_steps_yield_no_network_claim(db_session: Session) -> None:
    """Without any network tool the capture still asserts no-network."""
    flow = TaskFlow(
        id="flow-local-1",
        owner_id="owner-42",
        goal="write local notes",
        status=TaskState.DONE.value,
        tool_name="workspace.write_text",
        target_path="notes.md",
    )
    step1 = FlowStep(
        id="step-l1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="workspace.write_text",
        arguments={"path": "notes.md", "content": "ok"},
    )
    db_session.add_all([flow, step1])
    db_session.commit()

    card = build_card_from_trajectory(db_session, flow)

    assert ClaimKind.NO_NETWORK in {claim.kind for claim in card.claims}


def test_secret_like_values_are_redacted(tmp_path: Path, db_session: Session) -> None:
    secret_key = "sk-" + "a" * 30
    flow = TaskFlow(
        id="flow-secret-1",
        owner_id="owner-42",
        goal=f"write api key {secret_key}",
        status=TaskState.DONE.value,
        tool_name="workspace.write_text",
        target_path="config.txt",
    )
    step1 = FlowStep(
        id="step-s1",
        task_id=flow.id,
        step_number=1,
        status=StepState.COMPLETED.value,
        tool_name="workspace.write_text",
        arguments={"path": "config.txt", "content": f"key = {secret_key}"},
    )
    artifact = Artifact(
        task_id=flow.id,
        step_id=step1.id,
        path="config.txt",
        sha256="abc123sha",
        size=100,
        verified=True,
    )
    db_session.add_all([flow, step1, artifact])
    db_session.commit()

    registry = SkillsRegistry(db_session)
    skill = capture_from_flow(db_session, flow.id, state_root=str(tmp_path), registry=registry)

    body = registry.get_card_body(skill, str(tmp_path)).decode()
    assert secret_key not in body
