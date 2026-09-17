from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.task_registry import (
    TaskListFilter,
    TaskRegistry,
    TaskRegistryError,
    TaskRegistryNotFound,
)
from antigona.database import Database
from antigona.durable.state_machine import InvalidTransition
from antigona.models import TaskState


def test_create_get_and_list(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_main.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="write result",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        loaded = registry.get(created.task_id)
        listed = registry.list(TaskListFilter(owner_id="owner-1"))

        assert loaded.task_id == created.task_id
        assert loaded.status is TaskState.RECEIVED
        assert len(listed) == 1
        assert listed[0].task_id == created.task_id


def test_create_idempotency_returns_existing_task(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_idempotency.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        first = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        second = registry.create(
            owner_id="owner-1",
            goal="another-goal",
            idempotency_key="idem-1",
            correlation_id="corr-2",
        )

        assert second.task_id == first.task_id


def test_update_status_bumps_revision_and_persists_state(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_update.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        updated = registry.update_status(
            created.task_id,
            TaskState.QUEUED,
            actor="gateway",
            correlation_id="corr-2",
        )

        assert updated.status is TaskState.QUEUED
        assert updated.revision == created.revision + 1


def test_update_status_rejects_terminal_transition(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_terminal.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        registry.update_status(
            created.task_id,
            TaskState.CANCELLED,
            actor="gateway",
            correlation_id="corr-2",
        )

        with pytest.raises(InvalidTransition):
            registry.update_status(
                created.task_id,
                TaskState.QUEUED,
                actor="gateway",
                correlation_id="corr-3",
            )


def test_update_status_fail_closed_on_missing_actor_or_correlation(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_fail_closed.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        created = registry.create(
            owner_id="owner-1",
            goal="goal",
            idempotency_key="idem-1",
            correlation_id="corr-1",
        )
        with pytest.raises(TaskRegistryError):
            registry.update_status(
                created.task_id,
                TaskState.QUEUED,
                actor="",
                correlation_id="corr-2",
            )
        with pytest.raises(TaskRegistryError):
            registry.update_status(
                created.task_id,
                TaskState.QUEUED,
                actor="gateway",
                correlation_id="",
            )


def test_get_unknown_task_fails_closed(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'task_registry_notfound.db'}")
    db.create_all()

    with db.session_factory() as session:
        registry = TaskRegistry(session)
        with pytest.raises(TaskRegistryNotFound):
            registry.get("does-not-exist")
