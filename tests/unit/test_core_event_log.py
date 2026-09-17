from __future__ import annotations

from pathlib import Path

from antigona.core.event_log import EventLog
from antigona.database import Database
from antigona.models import TaskFlow, TaskState


def _create_task(db: Database) -> str:
    with db.session_factory() as session:
        task = TaskFlow(
            owner_id="owner-1",
            goal="goal",
            target_path=".",
            content="",
            idempotency_key="idem-1",
            tool_name="workspace.write_text",
            tool_arguments={},
            status=TaskState.RECEIVED.value,
        )
        session.add(task)
        session.commit()
        return task.id


def test_replay_and_reconstruct(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'eventlog_replay.db'}")
    db.create_all()
    task_id = _create_task(db)

    with db.session_factory() as session:
        log = EventLog(session)
        log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=None,
            to_state=TaskState.RECEIVED,
            reason="created",
            actor="registry",
            correlation_id="corr-1",
        )
        log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-2",
        )
        log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=TaskState.QUEUED,
            to_state=TaskState.PLANNING,
            reason="planning",
            actor="worker",
            correlation_id="corr-3",
        )
        session.commit()

    with db.session_factory() as session:
        log = EventLog(session)
        replayed = log.replay(task_id)
        assert [record.to_state for record in replayed] == [
            TaskState.RECEIVED,
            TaskState.QUEUED,
            TaskState.PLANNING,
        ]
        assert log.reconstruct(task_id, TaskState.RECEIVED) is TaskState.PLANNING


def test_duplicate_append_idempotent_same_content_and_reject_different(
    tmp_path: Path,
) -> None:
    """Duplicate resistance: same (task_id, correlation_id) + same content ->
    idempotent (returns existing seq); DIFFERENT content -> fail closed
    (security fix: stale/malicious replay must not mask the real event)."""
    db = Database(f"sqlite:///{tmp_path / 'eventlog_dup_failclosed.db'}")
    db.create_all()
    task_id = _create_task(db)

    import pytest

    with db.session_factory() as session:
        log = EventLog(session)
        seq1 = log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-dup",
        )
        seq2 = log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-dup",
        )
        assert seq2 == seq1
        with pytest.raises(ValueError, match="refusing replay"):
            log.append(
                task_id=task_id,
                entity_id=task_id,
                entity_type="task",
                from_state=TaskState.QUEUED,
                to_state=TaskState.DONE,
                reason="forged",
                actor="attacker",
                correlation_id="corr-dup",
            )
        session.commit()

def test_reconstruct_is_deterministic(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'eventlog_determinism.db'}")
    db.create_all()
    task_id = _create_task(db)

    with db.session_factory() as session:
        log = EventLog(session)
        log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=None,
            to_state=TaskState.RECEIVED,
            reason="created",
            actor="registry",
            correlation_id="corr-a",
        )
        log.append(
            task_id=task_id,
            entity_id=task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-b",
        )
        session.commit()

    with db.session_factory() as session:
        log = EventLog(session)
        first = log.reconstruct(task_id, TaskState.RECEIVED)
        second = log.reconstruct(task_id, TaskState.RECEIVED)
        assert first is second is TaskState.QUEUED


def test_event_log_api_is_append_only_surface() -> None:
    assert not hasattr(EventLog, "update")
    assert not hasattr(EventLog, "delete")
