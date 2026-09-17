from __future__ import annotations

from datetime import UTC

import pytest

from antigona.database import Database
from antigona.models import FlowStep, StepState, TaskState
from antigona.replay import (
    ReplayEngine,
    ReplayTaskNotFound,
    ReplayTimelineEntry,
    StepReplay,
    TransitionReplay,
)
from antigona.repository import CreateTask, TaskRepository


def _create_test_flow(session, owner_id: str = "owner-replay", goal: str = "Generate report", idempotency_key: str = "replay-idem-1"):
    repo = TaskRepository(session)
    task, _ = repo.create(
        CreateTask(
            owner_id=owner_id,
            goal=goal,
            path="report.txt",
            content="hello",
            idempotency_key=idempotency_key,
        )
    )
    # Transition task through states
    repo.transition(task, TaskState.QUEUED, reason="queued in line", actor="gateway")
    repo.transition(task, TaskState.PLANNING, reason="planner starting", actor="worker")
    repo.transition(task, TaskState.TOOL_EXECUTING, reason="execution started", actor="worker")

    # Add steps
    step = FlowStep(
        task_id=task.id,
        index=1,
        title="Write file step",
        status=StepState.COMPLETED.value,
        input={"action": "write", "path": "report.txt"},
        output={"bytes_written": 5},
    )
    session.add(step)
    session.commit()
    return task


def test_get_trajectory_returns_full_state() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)
        trajectory = engine.get_trajectory(task.id)

        assert trajectory.task_id == task.id
        assert trajectory.goal == "Generate report"
        assert trajectory.status == TaskState.TOOL_EXECUTING.value
        assert len(trajectory.transitions) >= 3
        assert len(trajectory.steps) >= 1
        assert trajectory.steps[-1].title == "Write file step"

        # Verify to_dict output
        d = trajectory.to_dict()
        assert isinstance(d, dict)
        assert d["task_id"] == task.id
        assert d["goal"] == "Generate report"


def test_get_trajectory_owner_isolation() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session, owner_id="owner-alice")
        engine = ReplayEngine(session)

        # Correct owner should work
        trajectory = engine.get_trajectory(task.id, owner_id="owner-alice")
        assert trajectory.task_id == task.id

        # Wrong owner should raise ReplayTaskNotFound (404 semantics)
        with pytest.raises(ReplayTaskNotFound):
            engine.get_trajectory(task.id, owner_id="owner-eve")


def test_get_trajectory_not_found() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        engine = ReplayEngine(session)
        with pytest.raises(ReplayTaskNotFound, match="nonexistent"):
            engine.get_trajectory("nonexistent-id")


def test_get_timeline_is_sorted() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)
        entries = engine.get_timeline(task.id)

        # Should have entries
        assert len(entries) > 0

        # All entries should be ReplayTimelineEntry
        for e in entries:
            assert isinstance(e, ReplayTimelineEntry)
            assert e.type in ("transition", "rejected", "artifact")
            assert e.timestamp

        # Should be sorted by timestamp
        timestamps = [e.timestamp for e in entries]
        assert timestamps == sorted(timestamps)


def test_filter_by_actor() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)

        # Filter by actor="worker"
        trajectory = engine.get_trajectory(task.id, actor="worker")
        for tr in trajectory.transitions:
            assert tr.actor == "worker"

        # Filter by actor="gateway"
        trajectory = engine.get_trajectory(task.id, actor="gateway")
        for tr in trajectory.transitions:
            assert tr.actor == "gateway"


def test_filter_by_entity_type() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)

        # Filter by entity_type="task"
        trajectory = engine.get_trajectory(task.id, entity_type="task")
        for tr in trajectory.transitions:
            assert tr.entity_type == "task"

        # Should also get task-level transitions for entity_type="step"
        # (no step-level transitions in our test data, so empty is expected)
        trajectory = engine.get_trajectory(task.id, entity_type="step")
        # The steps themselves are not transitions, so step transitions may be empty


def test_filter_by_time_range() -> None:
    from datetime import datetime

    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)

        # Use a wide time range that should include all
        from_dt = datetime(2020, 1, 1, tzinfo=UTC)
        to_dt = datetime(2030, 12, 31, tzinfo=UTC)

        trajectory = engine.get_trajectory(task.id, from_dt=from_dt, to_dt=to_dt)
        assert len(trajectory.transitions) >= 3

        # Use a range that should exclude everything
        from_dt_future = datetime(2099, 1, 1, tzinfo=UTC)
        to_dt_future = datetime(2099, 12, 31, tzinfo=UTC)
        trajectory = engine.get_trajectory(task.id, from_dt=from_dt_future, to_dt=to_dt_future)
        assert len(trajectory.transitions) == 0


def test_get_rejected_transitions() -> None:
    """Test that get_rejected_transitions returns empty (no rejected in test data)."""
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)
        rejected = engine.get_rejected_transitions(task.id)
        # Our test data has no rejected transitions
        assert isinstance(rejected, list)
        assert len(rejected) == 0  # No rejected in clean test flow


def test_render_replay_text_contains_fields() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)

        text = engine.render_replay_text(task.id)
        assert "Flow Replay:" in text
        assert task.goal in text or "Generate report" in text
        assert "RECEIVED" in text
        assert "Write file step" in text


def test_step_replay_dataclass() -> None:
    step = StepReplay(
        id="step-001",
        index=0,
        title="Write file",
        status="COMPLETED",
        input={"action": "write"},
        output={"bytes": 10},
        retries=0,
    )
    assert step.id == "step-001"
    assert step.index == 0
    assert step.title == "Write file"
    assert step.status == "COMPLETED"
    assert step.input == {"action": "write"}
    assert step.output == {"bytes": 10}
    assert step.retries == 0


def test_transition_replay_dataclass() -> None:
    tr = TransitionReplay(
        id=1,
        entity_id="task-001",
        entity_type="task",
        from_state="RECEIVED",
        to_state="QUEUED",
        reason="queued",
        actor="gateway",
        created_at="2026-07-26T10:00:00",
    )
    assert tr.id == 1
    assert tr.entity_id == "task-001"
    assert tr.entity_type == "task"
    assert tr.from_state == "RECEIVED"
    assert tr.to_state == "QUEUED"
    assert tr.reason == "queued"
    assert tr.actor == "gateway"


def test_render_timeline_text() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)

        text = engine.render_timeline_text(task.id)
        assert "Timeline for" in text
        assert "PLANNING" in text and "→" in text


def test_to_json_method() -> None:
    import json

    db = Database("sqlite:///:memory:")
    db.create_all()

    with db.session_factory() as session:
        task = _create_test_flow(session)
        engine = ReplayEngine(session)
        trajectory = engine.get_trajectory(task.id)

        json_str = engine.to_json(trajectory)
        parsed = json.loads(json_str)
        assert parsed["task_id"] == task.id
        assert parsed["goal"] == "Generate report"
