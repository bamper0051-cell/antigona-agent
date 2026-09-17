from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import TaskState
from antigona.orchestrator import Orchestrator
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier_client import VerifierClient
from antigona.verifier_service import create_verifier_app


class StubVerifier(VerifierClient):
    def __init__(self, decision: str = "DONE") -> None:
        self.decision = decision

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return self.decision


def repo(tmp_path: Path) -> tuple[Database, str]:
    database = Database(f"sqlite:///{tmp_path/'orch.db'}")
    database.create_all()
    with database.session_factory() as session:
        task, _ = TaskRepository(session).create(CreateTask("owner", "goal", "proof.txt", "data", "key"))
        DurableQueue = __import__("antigona.queue", fromlist=["DurableQueue"]).DurableQueue
        DurableQueue(session).enqueue(task)
    return database, task.id


def approve(task: Any, repository: TaskRepository) -> None:
    # Simulate the gateway: request a durable approval, then approve it, so the
    # orchestrator's PLANNING branch proceeds past WAITING_APPROVAL. The approval
    # flow itself is never bypassed; we only drive the legitimate gateway decision.
    ap = repository.request_approval(repository.get(task))
    if ap.decision == "PENDING":
        repository.decide_approval(repository.get(task), ap.id, "owner", True)


class VerifierHarness:
    """The verifier service is the ONLY component allowed to finalize DONE.

    A stub verifier cannot reach DONE (hard gate), so the happy-path test drives the
    real verifier app over an in-process client. The orchestrator's approval/execution
    flow is unchanged; only the legitimate verifier decision is exercised.
    """

    def __init__(self, database: Database, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
        self.database_url = database.engine.url.render_as_string(hide_password=False)
        self.client = TestClient(
            create_verifier_app(self.database_url, "secret", deterministic_test_judge())
        )
        self.client.__enter__()

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        seed_private_criteria(self.database_url, task_id)
        response = self.client.post(
            "/verify", headers={"Authorization": "Bearer secret"}, json={"task_id": task_id, "correlation_id": correlation_id}
        )
        response.raise_for_status()
        return str(response.json()["decision"])


def run(task: Any, verifier: StubVerifier, tmp_path: Path, **kw: Any) -> Any:
    settings = Settings(f"sqlite:///{tmp_path/'x.db'}", tmp_path / "ws", test_mode=True)
    tool = WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True), settings.tool_timeout_seconds)
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        repository = TaskRepository(session)
        # Approve any pending approval so the orchestrator can proceed to execution.
        approve(task, repository)
        orch = Orchestrator(session, tool, verifier, lease_seconds=1, **kw)
        return orch.run(repository.get(task), "worker-1")


def test_full_happy_path_reaches_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, task_id = repo(tmp_path)
    verifier = VerifierHarness(database, tmp_path / "ws", monkeypatch)
    settings = Settings(f"sqlite:///{tmp_path/'x.db'}", tmp_path / "ws", test_mode=True)
    tool = WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True), settings.tool_timeout_seconds)
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        repository = TaskRepository(session)
        approve(task_id, repository)
        orch = Orchestrator(session, tool, verifier, lease_seconds=1)
        result = orch.run(repository.get(task_id), "worker-1")
    assert result.status == TaskState.DONE.value
    # Verifier-only DONE hard gate: only the verifier service may finalize.
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        transitions = TaskRepository(session).get(task_id).transitions
        assert any(t.to_state == "DONE" and t.actor == "verifier-service" for t in transitions)


def test_verifier_replan_on_mismatch(tmp_path: Path) -> None:
    database, task_id = repo(tmp_path)
    result = run(task_id, StubVerifier("REPLAN"), tmp_path)
    assert result.status == TaskState.FAILED.value


def test_retry_on_retryable_failure(tmp_path: Path) -> None:
    database, task_id = repo(tmp_path)
    settings = Settings(f"sqlite:///{tmp_path/'x.db'}", tmp_path / "ws", test_mode=True)

    class FailingTool(WorkspaceFileTool):
        def execute(self, arguments: Any) -> Any:
            from antigona.contracts import ToolResult
            return ToolResult(False, "failed", error="transient", retryable=True)

    tool = FailingTool(InProcessTestBackend(tmp_path / "ws", test_mode=True), settings.tool_timeout_seconds)
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        repository = TaskRepository(session)
        approve(task_id, repository)
        orch = Orchestrator(session, tool, StubVerifier("DONE"), lease_seconds=1, max_retries=2)
        result = orch.run(repository.get(task_id), "worker-1")
    assert result.status == TaskState.FAILED.value


def test_sticky_cancellation_short_circuits(tmp_path: Path) -> None:
    database, task_id = repo(tmp_path)
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        task = TaskRepository(session).get(task_id)
        TaskRepository(session).cancel(task)
    settings = Settings(f"sqlite:///{tmp_path/'x.db'}", tmp_path / "ws", test_mode=True)
    tool = WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True), settings.tool_timeout_seconds)
    with Database(f"sqlite:///{tmp_path/'orch.db'}").session_factory() as session:
        repository = TaskRepository(session)
        orch = Orchestrator(session, tool, StubVerifier("DONE"), lease_seconds=1)
        result = orch.run(repository.get(task_id), "worker-1")
    assert result.status == TaskState.CANCELLED.value
