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
    BudgetLimitExceeded,
    DepthLimitExceeded,
    Orchestrator,
    aggregate_child_results,
    spawn_child_flow,
)
from antigona.pipeline import CompletionVerifier
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier_service import create_verifier_app


class StubVerifier(CompletionVerifier):
    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return "DONE"


class StubReplanVerifier(CompletionVerifier):
    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return "REPLAN"


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


def approve_task(task_id: str, repository: TaskRepository) -> None:
    task = repository.get(task_id)
    ap = repository.request_approval(task)
    if ap.decision == "PENDING":
        repository.decide_approval(task, ap.id, "owner", True)


def test_parent_spawns_child_flows_and_aggregates_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "subagents.db"
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

        child1 = spawn_child_flow(
            orch,
            parent,
            goal="Subtask 1",
            target_path="child1.txt",
            content="child1 content",
        )
        child2 = spawn_child_flow(
            orch,
            parent,
            goal="Subtask 2",
            target_path="child2.txt",
            content="child2 content",
        )

        assert child1.parent_id == parent.id
        assert child2.parent_id == parent.id
        assert child1.depth == 1
        assert child2.depth == 1

        # Run child 1 through Orchestrator & Verifier
        approve_task(child1.id, repository)
        c1_res = orch.run(child1, "worker-1")
        assert c1_res.status == TaskState.DONE.value

        # Run child 2 through Orchestrator & Verifier
        approve_task(child2.id, repository)
        c2_res = orch.run(child2, "worker-1")
        assert c2_res.status == TaskState.DONE.value

        # Aggregate child results
        agg = aggregate_child_results(orch, parent)
        assert agg["parent_id"] == parent.id
        assert agg["total_children"] == 2
        assert agg["done_count"] == 2
        assert agg["failed_count"] == 0
        assert agg["all_done"] is True
        assert len(agg["children"]) == 2


def test_depth_limit_blocks_deep_subagents(tmp_path: Path) -> None:
    db_path = tmp_path / "subagents_depth.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), 10)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "p.txt", "content", "parent-idem")
        )
        parent.max_depth = 1
        repository.commit()

        orch = Orchestrator(session, tool, StubVerifier())

        child1 = spawn_child_flow(
            orch, parent, goal="Child Goal", target_path="c.txt", content="c content"
        )
        assert child1.depth == 1

        # Attempting to spawn a child from child1 should exceed max_depth=1
        with pytest.raises(DepthLimitExceeded) as exc_info:
            spawn_child_flow(
                orch, child1, goal="Grandchild Goal", target_path="gc.txt", content="gc content"
            )
        assert "Depth limit exceeded" in str(exc_info.value)


def test_budget_limit_blocks_excessive_child_tasks(tmp_path: Path) -> None:
    db_path = tmp_path / "subagents_budget.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), 10)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "p.txt", "content", "parent-idem")
        )
        parent.max_child_budget = 2
        repository.commit()

        orch = Orchestrator(session, tool, StubVerifier())

        spawn_child_flow(orch, parent, goal="Child 1", target_path="c1.txt", content="c1")
        spawn_child_flow(orch, parent, goal="Child 2", target_path="c2.txt", content="c2")

        # Spawning 3rd child exceeds budget of 2
        with pytest.raises(BudgetLimitExceeded) as exc_info:
            spawn_child_flow(orch, parent, goal="Child 3", target_path="c3.txt", content="c3")
        assert "budget exceeded" in str(exc_info.value)


def test_child_inherits_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "subagents_owner.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), 10)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-xyz", "Parent Goal", "p.txt", "content", "parent-idem")
        )
        orch = Orchestrator(session, tool, StubVerifier())

        child = spawn_child_flow(
            orch, parent, goal="Child", target_path="c.txt", content="c"
        )
        # The child always inherits owner and the parent's depth/budget envelope.
        assert child.owner_id == parent.owner_id == "owner-xyz"
        assert child.max_depth == parent.max_depth
        assert child.max_child_budget == parent.max_child_budget


def test_child_failure_isolated_from_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "subagents_isolation.db"
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

        ok_child = spawn_child_flow(
            orch, parent, goal="Ok", target_path="ok.txt", content="ok content"
        )
        # A child whose verifier rejects transitions to FAILED. Isolation means
        # that terminal failure does not roll back the sibling or the parent.
        fail_child = spawn_child_flow(
            orch, parent, goal="Fail", target_path="fail.txt", content="fail content"
        )

        approve_task(ok_child.id, repository)
        assert orch.run(ok_child, "worker-1").status == TaskState.DONE.value

        # Drive the failing child with a verifier that always requests a replan.
        fail_orch = Orchestrator(session, tool, StubReplanVerifier(), lease_seconds=1)
        approve_task(fail_child.id, repository)
        assert fail_orch.run(fail_child, "worker-1").status == TaskState.FAILED.value

        # Parent is untouched by the child's failure.
        parent_reloaded = repository.get(parent.id)
        assert parent_reloaded.status != TaskState.FAILED.value

        agg = aggregate_child_results(orch, parent)
        assert agg["total_children"] == 2
        assert agg["done_count"] == 1
        assert agg["failed_count"] == 1
        assert agg["all_done"] is False


def test_spawn_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "subagents_idem.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), 10)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "p.txt", "content", "parent-idem")
        )
        orch = Orchestrator(session, tool, StubVerifier())

        first = spawn_child_flow(
            orch, parent, goal="Child", target_path="c.txt", content="c",
            idempotency_key="child-fixed-key",
        )
        # A retry with the same explicit key returns the same row, no duplicate.
        second = spawn_child_flow(
            orch, parent, goal="Child", target_path="c.txt", content="c",
            idempotency_key="child-fixed-key",
        )
        assert first.id == second.id

        agg = aggregate_child_results(orch, parent)
        assert agg["total_children"] == 1


def test_aggregate_empty_children(tmp_path: Path) -> None:
    db_path = tmp_path / "subagents_empty.db"
    ws_path = tmp_path / "ws"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    tool = WorkspaceFileTool(InProcessTestBackend(ws_path, test_mode=True), 10)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        parent, _ = repository.create(
            CreateTask("owner-1", "Parent Goal", "p.txt", "content", "parent-idem")
        )
        orch = Orchestrator(session, tool, StubVerifier())

        agg = aggregate_child_results(orch, parent)
        assert agg["parent_id"] == parent.id
        assert agg["total_children"] == 0
        assert agg["done_count"] == 0
        assert agg["failed_count"] == 0
        assert agg["pending_count"] == 0
        assert agg["all_done"] is False
        assert agg["children"] == []
