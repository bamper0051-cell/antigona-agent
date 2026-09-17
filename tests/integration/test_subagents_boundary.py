"""Integration boundaries for P3.1 subagents: parallel workstreams and mixed outcomes.

These drive real children through the ``Orchestrator``/``Verifier`` path (not stubs
at the transition layer) and assert that aggregation reflects the true terminal
state of each independent child flow. Isolation is proved behaviourally: a failing
child never rolls back a sibling that already reached DONE.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import TaskState
from antigona.orchestrator import (
    Orchestrator,
    aggregate_child_results,
    spawn_child_flow,
)
from antigona.pipeline import CompletionVerifier
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier_service import create_verifier_app


class VerifierHarness:
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
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        response.raise_for_status()
        return str(response.json()["decision"])


class StubReplanVerifier(CompletionVerifier):
    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return "REPLAN"


def approve_task(task_id: str, repository: TaskRepository) -> None:
    task = repository.get(task_id)
    ap = repository.request_approval(task)
    if ap.decision == "PENDING":
        repository.decide_approval(task, ap.id, "owner", True)


def test_parallel_workstream_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "parallel.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    verifier = VerifierHarness(database, ws_path, monkeypatch)
    settings = Settings(f"sqlite:///{db_path}", ws_path, test_mode=True)
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), settings.tool_timeout_seconds)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "parent.txt", "parent content", "parent-idem")
        )
        orch = Orchestrator(session, tool, verifier, lease_seconds=1)

        children = [
            spawn_child_flow(
                orch, parent, goal=f"Subtask {i}", target_path=f"c{i}.txt", content=f"c{i} content"
            )
            for i in range(3)
        ]
        assert all(c.parent_id == parent.id for c in children)
        assert all(c.depth == 1 for c in children)

        for idx, child in enumerate(children):
            approve_task(child.id, repository)
            assert orch.run(child, f"worker-{idx}").status == TaskState.DONE.value

        agg = aggregate_child_results(orch, parent)
        assert agg["total_children"] == 3
        assert agg["done_count"] == 3
        assert agg["failed_count"] == 0
        assert agg["pending_count"] == 0
        assert agg["all_done"] is True
        # Every DONE child carries at least one verified artifact for the parent.
        assert all(c["artifacts"] for c in agg["children"])


def test_mixed_outcomes_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "mixed.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    verifier = VerifierHarness(database, ws_path, monkeypatch)
    settings = Settings(f"sqlite:///{db_path}", ws_path, test_mode=True)
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), settings.tool_timeout_seconds)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "parent.txt", "parent content", "parent-idem")
        )
        done_orch = Orchestrator(session, tool, verifier, lease_seconds=1)
        fail_orch = Orchestrator(session, tool, StubReplanVerifier(), lease_seconds=1)

        c1 = spawn_child_flow(done_orch, parent, goal="A", target_path="a.txt", content="A content")
        c2 = spawn_child_flow(done_orch, parent, goal="B", target_path="b.txt", content="B content")
        c3 = spawn_child_flow(done_orch, parent, goal="C", target_path="c.txt", content="C content")

        approve_task(c1.id, repository)
        assert done_orch.run(c1, "worker-1").status == TaskState.DONE.value
        approve_task(c2.id, repository)
        assert done_orch.run(c2, "worker-2").status == TaskState.DONE.value
        # c3 fails verification -> FAILED, must not disturb c1/c2 or the parent.
        approve_task(c3.id, repository)
        assert fail_orch.run(c3, "worker-3").status == TaskState.FAILED.value

        parent_reloaded = repository.get(parent.id)
        assert parent_reloaded.status != TaskState.FAILED.value

        agg = aggregate_child_results(done_orch, parent)
        assert agg["total_children"] == 3
        assert agg["done_count"] == 2
        assert agg["failed_count"] == 1
        assert agg["pending_count"] == 0
        assert agg["all_done"] is False
