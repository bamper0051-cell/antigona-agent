from __future__ import annotations

from pathlib import Path

from antigona.core.event_log import EventLog
from antigona.core.task_registry import TaskRegistry
from antigona.database import Database
from antigona.models import TaskState


def test_registry_eventlog_transactional_flow(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'registry_eventlog.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        event_log = EventLog(session)

        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        assert created.revision == 0

        queued = registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-2",
        )
        planning = registry.update_status(
            created.task_id,
            TaskState.PLANNING,
            actor="worker",
            correlation_id="corr-3",
        )
        session.commit()

        assert queued.revision == 1
        assert planning.revision == 2

        replayed = event_log.replay(created.task_id)
        assert [event.to_state for event in replayed] == [
            TaskState.RECEIVED,
            TaskState.QUEUED,
            TaskState.PLANNING,
        ]
        assert event_log.reconstruct(created.task_id, TaskState.RECEIVED) is TaskState.PLANNING
        assert event_log.verify_materialized(created.task_id, TaskState.PLANNING)


def test_eventlog_duplicate_append_is_idempotent(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'registry_eventlog_duplicate.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        event_log = EventLog(session)
        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        seq1 = event_log.append(
            task_id=created.task_id,
            entity_id=created.task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-dup",
        )
        seq2 = event_log.append(
            task_id=created.task_id,
            entity_id=created.task_id,
            entity_type="task",
            from_state=TaskState.RECEIVED,
            to_state=TaskState.QUEUED,
            reason="queued",
            actor="gateway",
            correlation_id="corr-dup",
        )
        session.commit()

        # Same (task_id, correlation_id) + same content -> idempotent.
        assert seq1 == seq2
        assert len(event_log.replay(created.task_id)) == 2

        # Same correlation with DIFFERENT content -> fail closed (replay guard).
        import pytest

        with pytest.raises(ValueError, match="refusing replay"):
            event_log.append(
                task_id=created.task_id,
                entity_id=created.task_id,
                entity_type="task",
                from_state=TaskState.RECEIVED,
                to_state=TaskState.PLANNING,
                reason="forged",
                actor="worker",
                correlation_id="corr-dup",
            )
