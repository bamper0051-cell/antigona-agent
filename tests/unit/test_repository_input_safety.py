from __future__ import annotations

import json
import re
from collections.abc import Mapping

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.models import (
    Approval,
    Artifact,
    DeliveryOutbox,
    DurableOperation,
    FlowStep,
    QueueJob,
    StateTransition,
    TaskFlow,
)
from antigona.repository import CreateTask, TaskRepository

_PERSISTED_MODELS = (
    TaskFlow,
    FlowStep,
    Approval,
    Artifact,
    QueueJob,
    DurableOperation,
    StateTransition,
    DeliveryOutbox,
)


def _row_counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in _PERSISTED_MODELS
    }


@pytest.mark.parametrize(
    ("path", "content", "goal", "tool_name", "command", "marker"),
    [
        (
            ".env/PRECOMMIT_PATH_MARKER",
            "safe payload",
            "write a report",
            "workspace.write_text",
            (),
            "PRECOMMIT_PATH_MARKER",
        ),
        (
            "reports/out.txt",
            "API_KEY=PRECOMMIT_CONTENT_MARKER",
            "write a report",
            "workspace.write_text",
            (),
            "PRECOMMIT_CONTENT_MARKER",
        ),
        (
            "reports/out.txt",
            "safe payload",
            "Authorization: Bearer PRECOMMIT_GOAL_MARKER",
            "workspace.write_text",
            (),
            "PRECOMMIT_GOAL_MARKER",
        ),
        (
            "reports/out.txt",
            "safe payload",
            "run a safe command",
            "sandbox.shell",
            ("printenv", "PRECOMMIT_COMMAND_MARKER"),
            "PRECOMMIT_COMMAND_MARKER",
        ),
    ],
    ids=("path", "content", "goal", "command"),
)
def test_sensitive_create_rejects_before_any_row_or_pending_object(
    path: str,
    content: str,
    goal: str,
    tool_name: str,
    command: tuple[str, ...],
    marker: str,
) -> None:
    database = Database("sqlite:///:memory:")
    database.create_all()

    with database.session_factory() as session:
        repository = TaskRepository(session)
        before = _row_counts(session)

        with pytest.raises(ValueError) as raised:
            repository.create(
                CreateTask(
                    owner_id="owner",
                    goal=goal,
                    path=path,
                    content=content,
                    idempotency_key=f"reject-{marker}",
                    tool_name=tool_name,
                    command=command,
                )
            )

        assert str(raised.value) == "task input rejected by safety policy"
        assert marker not in str(raised.value)
        assert not session.new
        assert _row_counts(session) == before == {
            model.__tablename__: 0 for model in _PERSISTED_MODELS
        }


def test_safe_create_keeps_execution_fields_but_redacts_replay_and_approval_inputs() -> None:
    database = Database("sqlite:///:memory:")
    database.create_all()

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, created = repository.create(
            CreateTask(
                owner_id="owner",
                goal="write an ordinary report",
                path="reports/out.txt",
                content="ordinary safe payload",
                idempotency_key="safe-write",
            )
        )

        assert created is True
        assert task.target_path == "reports/out.txt"
        assert task.content == "ordinary safe payload"
        assert set(task.tool_arguments) == {"path", "arguments_sha256"}
        assert task.tool_arguments["path"] == "reports/out.txt"
        assert re.fullmatch(r"[0-9a-f]{64}", task.tool_arguments["arguments_sha256"])
        assert task.steps[0].input == {
            "tool_name": "workspace.write_text",
            "arguments_sha256": task.tool_arguments["arguments_sha256"],
        }

        approval = repository.request_approval(task)
        assert approval.arguments == {
            "tool_name": "workspace.write_text",
            "arguments_sha256": task.tool_arguments["arguments_sha256"],
        }
        serialized_approval = json.dumps(
            {"arguments": approval.arguments, "reason": approval.reason}, sort_keys=True
        )
        assert "ordinary safe payload" not in serialized_approval
        assert "reports/out.txt" not in serialized_approval

        repeated, repeated_created = repository.create(
            CreateTask(
                owner_id="owner",
                goal="write an ordinary report",
                path="reports/out.txt",
                content="ordinary safe payload",
                idempotency_key="safe-write",
            )
        )
        assert repeated_created is False
        assert repeated.id == task.id


def test_safe_shell_command_remains_available_only_where_execution_requires_it() -> None:
    database = Database("sqlite:///:memory:")
    database.create_all()

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                owner_id="owner",
                goal="print a fixed greeting",
                path="reports/shell.txt",
                content="hello",
                idempotency_key="safe-shell",
                tool_name="sandbox.shell",
                command=("printf", "hello"),
            )
        )

        assert task.tool_arguments["command"] == ["printf", "hello"]
        assert set(task.tool_arguments) == {"command", "arguments_sha256"}
        assert "command" not in task.steps[0].input

        approval = repository.request_approval(task)
        assert "command" not in approval.arguments
        assert approval.arguments["arguments_sha256"] == task.tool_arguments["arguments_sha256"]
        assert isinstance(approval.arguments, Mapping)
