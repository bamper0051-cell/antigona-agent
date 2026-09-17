from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.gateway.api import create_gateway_app
from antigona.models import TaskState
from antigona.orchestrator import Orchestrator
from antigona.queue import DurableQueue
from antigona.repository import TaskRepository
from antigona.verifier_service import create_verifier_app


class VerifierHarness:
    def __init__(self, database_url: str, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
        self._database_url = database_url
        self._client = TestClient(
            create_verifier_app(database_url, "secret", deterministic_test_judge())
        )
        self._client.__enter__()

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        seed_private_criteria(self._database_url, task_id)
        response = self._client.post(
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        response.raise_for_status()
        return str(response.json()["decision"])


def _auth(token: str = "gateway-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_stale_task_object_recovers_revision_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gateway mints the owner-decision grant through the canonical default
    # ApprovalGrantStore() (no explicit db_path). Redirect the project root so it
    # resolves inside tmp_path and never the live project grant database.
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    database_url = f"sqlite:///{tmp_path/'db_stale_task.sqlite'}"
    workspace = tmp_path / "workspace"
    settings = Settings(
        database_url=database_url,
        workspace=workspace,
        dev_tokens={"gateway-token": "owner-1"},
        sandbox_backend="inprocess",
        test_mode=True,
    )
    tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True))
    verifier = VerifierHarness(database_url, workspace, monkeypatch)
    gateway_app = create_gateway_app(settings)

    with TestClient(gateway_app) as gateway:
        created = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "stale-task-e2e-1"},
            json={
                # .env -> HIGH risk, approval required (canon P0: a plain in-workspace
                # write is LOW/auto-approved and would skip the WAITING_APPROVAL leg
                # this test exercises -- see tests/unit/test_failure_b_approval_resume.py).
                "goal": "create file after approval with stale object",
                "path": "stale_recovered.env",
                "content": "recovered from stale task object",
            },
        )
        assert created.status_code == 201
        flow_id = created.json()["id"]

        # 1. Worker claims job, plans, moves task to WAITING_APPROVAL
        stale_revision = 0
        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            result = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert result.status == TaskState.WAITING_APPROVAL.value
            job.status = "WAITING"
            session.commit()
            approval_id = result.approvals[0].id
            stale_revision = task.revision

        # 2. User approves via Gateway POST /approvals/{id}/decision
        decision_resp = gateway.post(
            f"/approvals/{approval_id}/decision",
            headers=_auth(),
            json={"approve": True},
        )
        assert decision_resp.status_code == 200
        assert decision_resp.json()["decision"] == "APPROVED"

        # 3. Worker re-claims job from queue, but uses stale task object (stale revision in memory)
        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None

            # Get task from repository and simulate stale revision attribute in memory
            task = TaskRepository(session).get(job.task_id)
            task.revision = stale_revision

            # Orchestrator runs with the task object whose revision attribute in memory is stale.
            # TaskRepository refreshes task from DB on transition/lease, avoiding CAS failure.
            result = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert result.status == TaskState.DONE.value
            queue.finish(job)

        # 4. Verify TaskFlow in DB reaches DONE without CAS failure
        with gateway_app.state.database.session_factory() as session:
            task = TaskRepository(session).get(flow_id)
            assert task.status == TaskState.DONE.value

        assert (workspace / "stale_recovered.env").read_text(encoding="utf-8") == "recovered from stale task object"
