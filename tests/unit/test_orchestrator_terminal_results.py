from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.contracts import ToolResult
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import DurableOperation, TaskState
from antigona.orchestrator import Orchestrator
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, TaskRepository
from antigona.result_safety import sanitize_result_text
from antigona.verifier_service import create_verifier_app


class RealVerifier:
    def __init__(
        self,
        database: Database,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
        self.database_url = database.engine.url.render_as_string(hide_password=False)
        self.workspace = workspace
        self.client = TestClient(
            create_verifier_app(
                self.database_url,
                "test-verifier-credential",
                deterministic_test_judge(),
            )
        )
        self.client.__enter__()

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        response = self.client.post(
            "/verify",
            headers={"Authorization": "Bearer test-verifier-credential"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        response.raise_for_status()
        return str(response.json()["decision"])


class RecordingShell:
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls = 0

    def execute(self, _arguments: Any) -> ToolResult:
        self.calls += 1
        return self.result


class RecordingVerifier:
    def __init__(self, decision: str = "DONE") -> None:
        self.decision = decision
        self.calls = 0

    def request_verification(self, _task_id: str, _correlation_id: str) -> str:
        self.calls += 1
        return self.decision


class RaisingShell:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls = 0

    def execute(self, _arguments: Any) -> ToolResult:
        self.calls += 1
        raise RuntimeError(self.marker)


class RaisingVerifier:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls = 0

    def request_verification(self, _task_id: str, _correlation_id: str) -> str:
        self.calls += 1
        raise RuntimeError(self.marker)


def create_shell_task(
    database: Database,
    *,
    command: tuple[str, ...],
    path: str = "stdout",
    content: str = "",
    key: str = "shell-task",
    bypass_creation_policy: bool = False,
) -> str:
    with database.session_factory() as session:
        create_path = "stdout" if bypass_creation_policy else path
        create_content = "" if bypass_creation_policy else content
        create_command = ("printf", "safe") if bypass_creation_policy else command
        task, _ = TaskRepository(session).create(
            CreateTask(
                "owner",
                "count bytes in workspace/file.txt",
                create_path,
                create_content,
                key,
                "sandbox.shell",
                create_command,
            )
        )
        DurableQueue(session).enqueue(task)
        repository = TaskRepository(session)
        approval = repository.request_approval(repository.get(task.id))
        if approval.decision == "PENDING":
            repository.decide_approval(repository.get(task.id), approval.id, "owner", True)
        if bypass_creation_policy:
            legacy = repository.get(task.id)
            legacy.target_path = path
            legacy.content = content
            legacy.tool_arguments = {"command": list(command)}
            session.commit()
        return task.id


def test_orchestrator_persists_sanitized_stdout_and_verifier_alone_reaches_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'orchestrator-result.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(
        InProcessTestBackend(workspace, test_mode=True),
        timeout_seconds=1,
    )
    raw = "42 workspace/file.txt\nAPI_KEY=synthetic-api-key"
    shell = RecordingShell(ToolResult(True, "completed", {"output": raw}))
    task_id = create_shell_task(database, command=("wc", "-c", "workspace/file.txt"))
    expected = sanitize_result_text(raw)
    assert expected is not None
    seed_private_criteria(
        database.engine.url.render_as_string(hide_password=False),
        task_id,
        expected,
    )
    verifier = RealVerifier(database, workspace, monkeypatch)

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.DONE.value
    assert shell.calls == 1
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        operation = session.scalar(
            select(DurableOperation).where(DurableOperation.task_id == task_id)
        )
        assert operation is not None and operation.result is not None
        serialized = repr(operation.result)
        assert set(operation.request) == {"arguments_sha256", "path", "content_sha256"}
        assert "wc" not in repr(operation.request)
        assert "synthetic-api-key" not in serialized
        assert "[REDACTED]" in serialized
        assert operation.result["tool_result"]["stdout_preview"].startswith("42 ")
        assert task.steps[0].output is not None
        assert task.artifacts[0].path.startswith(".antigona-results/")
        assert task.artifacts[0].verified is True
        done = [t for t in task.transitions if t.to_state == TaskState.DONE.value]
        assert len(done) == 1
        assert done[0].actor == "verifier-service"


def test_sensitive_command_is_blocked_before_tool_and_text_is_omitted(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'sensitive.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(
        ToolResult(True, "completed", {"output": "PASSWORD=synthetic-password"})
    )
    task_id = create_shell_task(
        database,
        command=("cat", ".env"),
        path=".env",
        key="sensitive-shell",
        bypass_creation_policy=True,
    )

    class NeverVerifier:
        def request_verification(self, _task_id: str, _correlation_id: str) -> str:
            raise AssertionError("blocked execution must never reach verifier")

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            NeverVerifier(),  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.BLOCKED.value
    assert shell.calls == 0
    assert result.steps[0].output is not None
    projection = result.steps[0].output["tool_result"]
    assert projection["blocked"] is True
    assert projection["text_omitted"] is True
    assert "stdout_preview" not in projection
    assert not result.artifacts
    assert not any(t.to_state == TaskState.DONE.value for t in result.transitions)


def test_failed_tool_does_not_persist_raw_stderr(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'failure.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    raw = "Traceback: password=synthetic-password"
    shell = RecordingShell(ToolResult(False, "failed", {"output": raw}, error=raw))
    task_id = create_shell_task(
        database,
        command=("false",),
        key="failed-shell",
    )

    class NeverVerifier:
        def request_verification(self, _task_id: str, _correlation_id: str) -> str:
            raise AssertionError("failed execution must never reach verifier")

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            NeverVerifier(),  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
            max_retries=0,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.FAILED.value
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        operation = session.scalar(
            select(DurableOperation).where(DurableOperation.task_id == task_id)
        )
        assert operation is not None and operation.result is not None
        persisted = repr(operation.result) + repr(task.steps[0].output) + repr(
            [t.reason for t in task.transitions]
        )
        assert "synthetic-password" not in persisted
        assert "Traceback" not in persisted
        assert "tool execution failed" in persisted


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/tmp/output.txt",
        "../output.txt",
        ".",
        ".envrc",
        ".token",
        r"\\server\share\output.txt",
        "C:relative.txt",
    ],
)
def test_unsafe_target_path_is_blocked_before_tool_and_verifier(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'blocked-path.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": "safe output"}))
    verifier = RecordingVerifier()
    task_id = create_shell_task(
        database,
        command=("printf", "safe"),
        path=unsafe_path,
        key="blocked-path",
        bypass_creation_policy=True,
    )

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.BLOCKED.value
    assert shell.calls == 0
    assert verifier.calls == 0
    assert not result.artifacts
    assert not any(item.to_state == TaskState.DONE.value for item in result.transitions)
    assert {item.reason for item in result.transitions if item.to_state == TaskState.BLOCKED.value} == {
        "Execution blocked by result safety policy"
    }


@pytest.mark.parametrize(
    "unsafe_argument",
    [
        "/tmp/input.txt",
        "../input.txt",
        ".",
        ".envrc",
        ".token",
        r"\\server\share\input.txt",
        "C:relative.txt",
    ],
)
def test_unsafe_command_path_is_blocked_before_tool_and_verifier(
    tmp_path: Path,
    unsafe_argument: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'blocked-command.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": "safe output"}))
    verifier = RecordingVerifier()
    task_id = create_shell_task(
        database,
        command=("cat", unsafe_argument),
        path="stdout",
        key="blocked-command",
        bypass_creation_policy=True,
    )

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.BLOCKED.value
    assert shell.calls == 0
    assert verifier.calls == 0
    assert not result.artifacts
    assert not any(item.to_state == TaskState.DONE.value for item in result.transitions)


def test_sensitive_content_is_blocked_before_tool_and_verifier(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'blocked-content.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": "safe output"}))
    verifier = RecordingVerifier()
    task_id = create_shell_task(
        database,
        command=("printf", "safe"),
        content="API_KEY=synthetic-content-marker",
        key="blocked-content",
        bypass_creation_policy=True,
    )

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.BLOCKED.value
    assert shell.calls == 0
    assert verifier.calls == 0
    assert not result.artifacts


def test_symlink_namespace_is_blocked_before_tool_and_verifier(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'blocked-symlink.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": "safe output"}))
    verifier = RecordingVerifier()
    task_id = create_shell_task(
        database,
        command=("printf", "safe"),
        path="linked/output.txt",
        key="blocked-symlink",
    )

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.BLOCKED.value
    assert shell.calls == 0
    assert verifier.calls == 0
    assert not result.artifacts


@pytest.mark.parametrize(
    "raw_output",
    [
        "",
        " \n\t ",
        'Traceback (most recent call last):\n  File "worker.py", line 1\nboom',
        "API_KEY=synthetic-fully-redacted-marker",
    ],
)
def test_unusable_stdout_cannot_materialize_artifact_or_reach_done(
    tmp_path: Path,
    raw_output: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'unusable-output.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": raw_output}))
    verifier = RecordingVerifier()
    task_id = create_shell_task(
        database,
        command=("printf", "safe"),
        key="unusable-output",
    )
    seed_private_criteria(
        database.engine.url.render_as_string(hide_password=False),
        task_id,
        "required nonempty result seeded before execution",
    )

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
            max_retries=0,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.FAILED.value
    assert shell.calls == 1
    assert verifier.calls == 0
    assert not result.artifacts
    assert not any(item.to_state == TaskState.DONE.value for item in result.transitions)


def test_tool_exception_is_fixed_mapped_without_marker_in_durable_state(tmp_path: Path) -> None:
    marker = "SYNTHETIC_TOOL_EXCEPTION_MARKER"
    database = Database(f"sqlite:///{tmp_path / 'tool-exception.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RaisingShell(marker)
    verifier = RecordingVerifier()
    task_id = create_shell_task(database, command=("false",), key="tool-exception")

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
            max_retries=0,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.FAILED.value
    assert verifier.calls == 0
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        operation = session.scalar(
            select(DurableOperation).where(DurableOperation.task_id == task_id)
        )
        assert operation is not None
        persisted = (
            repr(task.steps[0].output)
            + repr([item.reason for item in task.transitions])
            + repr(operation.result)
        )
    assert marker not in persisted
    assert "tool execution failed" in persisted


def test_verifier_exception_is_fixed_mapped_without_marker_or_done(tmp_path: Path) -> None:
    marker = "SYNTHETIC_VERIFIER_EXCEPTION_MARKER"
    database = Database(f"sqlite:///{tmp_path / 'verifier-exception.db'}")
    database.create_all()
    workspace = tmp_path / "workspace"
    file_tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 1)
    shell = RecordingShell(ToolResult(True, "completed", {"output": "usable result"}))
    verifier = RaisingVerifier(marker)
    task_id = create_shell_task(database, command=("printf", "safe"), key="verifier-exception")

    with database.session_factory() as session:
        result = Orchestrator(
            session,
            file_tool,
            verifier,  # type: ignore[arg-type]
            shell_tool=shell,  # type: ignore[arg-type]
            lease_seconds=1,
        ).run(TaskRepository(session).get(task_id), "worker-1")

    assert result.status == TaskState.FAILED.value
    assert verifier.calls == 1
    assert not any(item.to_state == TaskState.DONE.value for item in result.transitions)
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        operation = session.scalar(
            select(DurableOperation).where(DurableOperation.task_id == task_id)
        )
        assert operation is not None
        persisted = (
            repr(task.steps[0].output)
            + repr([item.reason for item in task.transitions])
            + repr(task.artifacts[0].evidence)
            + repr(operation.result)
        )
    assert marker not in persisted
    assert "verification service unavailable" in persisted
