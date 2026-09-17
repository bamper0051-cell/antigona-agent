"""Tests for P1-02: Orchestrator approval verification & one-shot consumption, time-bounding, and grant hashing."""
from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import TaskState, utcnow
from antigona.orchestrator import Orchestrator
from antigona.repository import CreateTask, TaskRepository
from antigona.security.approval_grant import ApprovalGrantStore
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


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    db_file = tmp_path / "test_approval.sqlite"
    grant_db_file = tmp_path / "grants.sqlite"
    monkeypatch.setattr("antigona.core.paths.database_path", lambda *a, **k: str(grant_db_file))

    db = Database(f"sqlite:///{db_file}")
    db.create_all()
    grant_store = ApprovalGrantStore(db_path=str(grant_db_file))
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True))
    verifier = VerifierHarness(f"sqlite:///{db_file}", ws, monkeypatch)
    return SimpleNamespace(db=db, grant_store=grant_store, ws=ws, tool=tool, verifier=verifier, tmp_path=tmp_path)


def test_one_shot_dispatch_consumes_grant(test_env):
    with test_env.db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="write safe file",
                path="reports/output.txt",
                content="hello world",
                idempotency_key=f"p1-oneshot-{uuid.uuid4()}",
                tool_name="workspace.write",
            )
        )
        approval = repo.request_approval(task)
        approval.decision = "PENDING"
        session.commit()

        # Decide approval
        decided = repo.decide_approval(task, approval.id, "owner", True)
        assert decided.decision == "APPROVED"
        assert decided.grant_token is not None
        assert len(decided.grant_token) == 64, "grant_token in DB must be a 64-char SHA256 hex string"

        orch = Orchestrator(session, test_env.tool, test_env.verifier)
        task = repo.get(task.id)

        # First run consumes the grant and drives to DONE
        res = orch.run(task, "worker-1")
        assert res.status == TaskState.DONE.value

        # Verify that the grant in store is now consumed
        grant = test_env.grant_store._load(test_env.grant_store._get_conn(), decided.grant_token)
        assert grant is not None
        assert grant.consumed_at is not None

        # A simulated second dispatch replay with the same consumed grant must be denied
        task2, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="write safe file 2",
                path="reports/output2.txt",
                content="hello again",
                idempotency_key=f"p1-replay-{uuid.uuid4()}",
                tool_name="workspace.write",
            )
        )
        # Attach the already-consumed grant
        app2 = repo.request_approval(task2)
        app2.decision = "APPROVED"
        app2.decided_by = "owner"
        app2.decided_at = utcnow()
        app2.grant_token = decided.grant_token
        session.commit()

        task2 = repo.get(task2.id)
        res2 = orch.run(task2, "worker-1")
        assert res2.status == TaskState.POLICY_DENIED.value
        assert "consumed" in str(res2.transitions[-1].reason).lower() or "invalid" in str(res2.transitions[-1].reason).lower()


def test_expired_approval_denied_by_orchestrator(test_env):
    with test_env.db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="write safe file",
                path="reports/output.txt",
                content="hello world",
                idempotency_key=f"p1-expired-{uuid.uuid4()}",
                tool_name="workspace.write",
            )
        )
        approval = repo.request_approval(task)
        approval.decision = "PENDING"
        session.commit()

        decided = repo.decide_approval(task, approval.id, "owner", True)
        # Manually backdate decided_at to 3700 seconds ago
        decided.decided_at = utcnow() - datetime.timedelta(seconds=3700)
        session.commit()

        orch = Orchestrator(session, test_env.tool, test_env.verifier)
        task = repo.get(task.id)
        res = orch.run(task, "worker-1")
        assert res.status == TaskState.POLICY_DENIED.value
        assert "expired" in str(res.transitions[-1].reason).lower()


def test_missing_grant_on_high_risk_denied(test_env):
    with test_env.db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="write safe file",
                path="reports/output.txt",
                content="hello world",
                idempotency_key=f"p1-nogrant-{uuid.uuid4()}",
                tool_name="workspace.write",
            )
        )
        approval = repo.request_approval(task)
        # Forged approved state without grant_token
        approval.decision = "APPROVED"
        approval.risk_level = "HIGH"
        approval.grant_token = None
        approval.decided_by = "owner"
        approval.decided_at = utcnow()
        session.commit()

        orch = Orchestrator(session, test_env.tool, test_env.verifier)
        task = repo.get(task.id)
        res = orch.run(task, "worker-1")
        assert res.status == TaskState.POLICY_DENIED.value
        assert "missing" in str(res.transitions[-1].reason).lower()


def test_legacy_raw_token_nulling_migration(tmp_path: Path):
    db_file = tmp_path / "legacy_migration.sqlite"
    db = Database(f"sqlite:///{db_file}")
    db.create_all()

    with db.session_factory() as session:
        repo = TaskRepository(session)
        t1, _ = repo.create(CreateTask(owner_id="o1", goal="g1", path="p1.txt", content="c1", idempotency_key="k1", tool_name="workspace.write"))
        t2, _ = repo.create(CreateTask(owner_id="o2", goal="g2", path="p2.txt", content="c2", idempotency_key="k2", tool_name="workspace.write"))
        a1 = repo.request_approval(t1)
        a2 = repo.request_approval(t2)
        a1.grant_token = "legacy_raw_token_short"
        a2.grant_token = "a" * 64
        session.commit()

    # Run migration
    db._ensure_approval_grant_column()

    with db.engine.connect() as conn:
        r1 = conn.exec_driver_sql("SELECT grant_token FROM approvals WHERE id = :id", {"id": a1.id}).scalar()
        r2 = conn.exec_driver_sql("SELECT grant_token FROM approvals WHERE id = :id", {"id": a2.id}).scalar()
        assert r1 is None, "Legacy raw token should have been nulled out"
        assert r2 == "a" * 64, "Valid 64-char token hash should remain intact"
