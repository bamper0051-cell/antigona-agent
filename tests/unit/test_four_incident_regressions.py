"""Minimal regression tests for the four forensic incidents (2026-08-15).

Each test captures the DESIRED contract. Tests marked RED fail until the
corresponding production fix lands; GREEN tests guard existing behaviour
and document the target query shape.

Incidents:
  1. Worker/Verifier liveness  — launcher completeness + stale heartbeat
  2. Dialogue session continuity — workspace artifact injection
  3. Event isolation           — /events scoping by flow
  4. Idempotency               — stable key to TaskSubmissionService.submit
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from antigona.context.builder import ContextBuilder
from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.health import heartbeat as hb
from antigona.models import StateTransition, TaskFlow


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    return database


# ── Incident 1: Worker/Verifier liveness ────────────────────────────────────


def test_run_sh_launches_all_services() -> None:
    """The canonical launcher must start every required service.

    The running gateway was launched standalone (bypassing run.sh), so the
    worker/verifier/delivery/bot/goal-engine were never started. Guard that
    run.sh still enumerates all six entrypoints.
    """
    launcher = (Path(__file__).parents[2] / "scripts/service_wrapper.sh").read_text(encoding="utf-8")
    markers = (
        "antigona.gateway",
        "antigona.verifier_service",
        "from antigona.worker import main",
        "from antigona.delivery_worker import main",
        "from antigona.channels.telegram.bot import main",
        "antigona.orchestration.service",
    )
    for marker in markers:
        assert marker in launcher, f"run.sh missing service marker: {marker}"


def test_stale_heartbeat_reports_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat older than the freshness window must read as down."""
    monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
    monkeypatch.delenv("ANTIGONA_HEALTH_DIR", raising=False)
    monkeypatch.setattr("antigona.core.paths.project_root", lambda: tmp_path)
    health_dir = tmp_path / ".health"
    health_dir.mkdir()
    (health_dir / "worker.json").write_text(
        '{"service": "worker", "pid": 1, "ts": 1.0, "status": "up"}',
        encoding="utf-8",
    )
    status = hb.read_status(window_seconds=15.0)
    assert status["worker"]["up"] is False


# ── Incident 2: dialogue session continuity ─────────────────────────────────


def test_context_builder_injects_workspace_artifact(tmp_path: Path) -> None:
    """RED: context must be able to include a produced workspace file.

    After "создай hello.txt" the next conversational turn must see the file's
    content. ContextBuilder.build currently has no artifact parameter, so this
    fails with TypeError until the fix adds it.
    """
    (tmp_path / "hello.txt").write_text("HELLO_FILE_MARKER_42", encoding="utf-8")
    builder = ContextBuilder()
    messages = builder.build(
        turn_buffer=[{"role": "user", "content": "прочитай этот файл"}],
        workspace_artifact=tmp_path / "hello.txt",
    )
    assert "HELLO_FILE_MARKER_42" in messages[0]["content"]


# ── Incident 3: event isolation ─────────────────────────────────────────────


def test_events_query_scopes_to_flow(db: Database) -> None:
    """Document the /events scoping contract.

    The current list_events (gateway/api.py) filters only by owner_id +
    after_seq, so one flow's events leak into another flow's dialogue UI.
    This test proves the desired scoped query returns only the target flow.
    """
    with db.session_factory() as session:
        session.add_all(
            [
                TaskFlow(
                    id="flow-a",
                    owner_id="owner-1",
                    goal="a",
                    target_path="stdout",
                    content="a",
                    idempotency_key="idem-a",
                ),
                TaskFlow(
                    id="flow-b",
                    owner_id="owner-1",
                    goal="b",
                    target_path="stdout",
                    content="b",
                    idempotency_key="idem-b",
                ),
            ]
        )
        session.add_all(
            [
                StateTransition(
                    task_id="flow-a",
                    entity_id="flow-a",
                    entity_type="task",
                    from_state="QUEUED",
                    to_state="RUNNING",
                    reason="",
                    actor="",
                    correlation_id="corr-a",
                ),
                StateTransition(
                    task_id="flow-b",
                    entity_id="flow-b",
                    entity_type="task",
                    from_state="QUEUED",
                    to_state="RUNNING",
                    reason="",
                    actor="",
                    correlation_id="corr-b",
                ),
            ]
        )
        session.commit()

    with db.session_factory() as session:
        # Current /events behaviour: owner-only => global stream (2 events).
        owner_only = session.scalars(
            select(StateTransition)
            .join(TaskFlow, StateTransition.task_id == TaskFlow.id)
            .where(TaskFlow.owner_id == "owner-1")
        ).all()
        assert len(owner_only) == 2

        # Desired: scoped by flow => only the target flow's events.
        scoped = session.scalars(
            select(StateTransition)
            .join(TaskFlow, StateTransition.task_id == TaskFlow.id)
            .where(
                TaskFlow.owner_id == "owner-1",
                StateTransition.task_id == "flow-a",
            )
        ).all()
        assert len(scoped) == 1
        assert scoped[0].task_id == "flow-a"


# ── Incident 4: idempotency ─────────────────────────────────────────────────


def test_submit_deduplicates_on_stable_key(db: Database) -> None:
    """RED: the same stable turn/correlation must not create a second TaskFlow.

    TaskSubmissionService.submit falls back to a random ``task-<uuid>`` key
    when idempotency_key is None, and brain._handle_task never forwards the
    stable turn_id/correlation_id. So a retry duplicates the flow.
    """
    svc = TaskSubmissionService(db)
    first = svc.submit(
        owner_id="owner-1",
        message="создай hello.txt",
        correlation_id="turn-abc",
        client="test",
    )
    second = svc.submit(
        owner_id="owner-1",
        message="создай hello.txt",
        correlation_id="turn-abc",
        client="test",
    )
    assert first["created"] is True
    assert second["created"] is False  # RED: currently True (duplicate flow)

    with db.session_factory() as session:
        flows = session.scalars(
            select(TaskFlow).where(TaskFlow.owner_id == "owner-1")
        ).all()
        assert len(flows) == 1  # RED: currently 2


def test_six_systemd_units_have_explicit_owned_exec_contract() -> None:
    root = Path(__file__).parents[2]
    expected = {"gateway": "antigona.gateway", "verifier": "antigona.verifier_service", "worker": "from antigona.worker import main", "delivery": "from antigona.delivery_worker import main", "bot": "from antigona.channels.telegram.bot import main", "orchestration": "antigona.orchestration.service"}
    wrapper = (root / "scripts/service_wrapper.sh").read_text(encoding="utf-8")
    # Portable hardened units must never hardcode the absolute host checkout
    # path; they address the tree solely through the @ANTIGONA_ROOT@ token.
    host_root = str(root)
    for role, marker in expected.items():
        unit = (root / "deploy/systemd" / f"antigona-{role}.service").read_text(encoding="utf-8")
        unit_lines = [line.strip() for line in unit.splitlines()]
        # (a) Hardened base unit: dedicated identity + placeholder-relative cwd.
        assert any(line.startswith("User=") and line != "User=" for line in unit_lines)
        assert any(line.startswith("Group=") and line != "Group=" for line in unit_lines)
        assert "WorkingDirectory=@ANTIGONA_ROOT@" in unit_lines
        # (a') The role's own launch contract must be present and exact.
        assert marker in unit
        # (a'') Portability: no absolute host checkout path may leak in.
        assert host_root not in unit
        # (b) The retired wrapper launcher survives as the legacy template and
        # still delegates to the wrapper for this role.
        legacy = (root / "deploy/systemd" / f"antigona-{role}.service.legacy.template").read_text(encoding="utf-8")
        assert f"service_wrapper.sh {role}" in legacy
        # (c) The wrapper itself still owns the role's marker.
        assert marker in wrapper
