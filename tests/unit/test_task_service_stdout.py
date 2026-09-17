"""TaskSubmissionService: shell tasks without explicit path → stdout target."""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.repository import TaskRepository


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    return database


def test_shell_task_without_path_uses_stdout(db: Database) -> None:
    svc = TaskSubmissionService(db)
    res = svc.submit(
        owner_id="owner-1",
        message="Выполни echo WIP-SHELL-CHECK",
        tool_name="sandbox.shell",
        command=("echo", "WIP-SHELL-CHECK"),
        idempotency_key="k-stdout-1",
        client="test",
    )
    assert res["created"] is True
    with db.session_factory() as session:
        task = TaskRepository(session).get(res["flow_id"])
        assert task.target_path == "stdout"


def test_write_task_keeps_default_path(db: Database) -> None:
    svc = TaskSubmissionService(db)
    res = svc.submit(
        owner_id="owner-1",
        message="запиши hello в notes.txt",
        idempotency_key="k-write-1",
        client="test",
    )
    assert res["created"] is True
    with db.session_factory() as session:
        task = TaskRepository(session).get(res["flow_id"])
        assert task.target_path == "task_output.txt"


def test_explicit_path_wins_for_shell_task(db: Database) -> None:
    svc = TaskSubmissionService(db)
    res = svc.submit(
        owner_id="owner-1",
        message="Выполни echo hi",
        tool_name="sandbox.shell",
        command=("echo", "hi"),
        path="out.txt",
        idempotency_key="k-path-1",
        client="test",
    )
    assert res["created"] is True
    with db.session_factory() as session:
        task = TaskRepository(session).get(res["flow_id"])
        assert task.target_path == "out.txt"
