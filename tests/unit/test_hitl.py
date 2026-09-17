from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
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
from antigona.worker.hitl import (
    ConfirmationPolicyMode,
    RiskLevel,
    check_approval_timeout,
    evaluate_risk,
    get_confirmation_policy,
    set_confirmation_policy,
)


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


def test_hitl_module_imports_and_policy_setting() -> None:
    orig = get_confirmation_policy()
    try:
        policy = set_confirmation_policy("HIGH_ONLY", timeout_seconds=10.0)
        assert policy.mode == ConfirmationPolicyMode.HIGH_ONLY
        assert policy.timeout_seconds == 10.0
        assert get_confirmation_policy().mode == ConfirmationPolicyMode.HIGH_ONLY

        assert policy.should_require_approval(RiskLevel.HIGH) is True
        assert policy.should_require_approval(RiskLevel.MEDIUM) is False
        assert policy.should_require_approval(RiskLevel.LOW) is False

        set_confirmation_policy(ConfirmationPolicyMode.NEVER)
        p_never = get_confirmation_policy()
        assert p_never.should_require_approval(RiskLevel.HIGH) is False
        assert p_never.should_require_approval(RiskLevel.MEDIUM) is False

        set_confirmation_policy(ConfirmationPolicyMode.ALWAYS)
        p_always = get_confirmation_policy()
        assert p_always.should_require_approval(RiskLevel.HIGH) is True
        assert p_always.should_require_approval(RiskLevel.MEDIUM) is True
        assert p_always.should_require_approval(RiskLevel.LOW) is False
    finally:
        set_confirmation_policy(orig)


def test_evaluate_risk_classifier_heuristics() -> None:
    risk, reason = evaluate_risk("web.fetch", {"url": "https://example.com"})
    assert risk == RiskLevel.HIGH
    assert "network" in reason.lower()

    risk, reason = evaluate_risk("sandbox.shell", {"command": ["sh", "-c", "rm -rf /"]})
    assert risk == RiskLevel.HIGH
    assert "destructive" in reason.lower()

    risk, reason = evaluate_risk("sandbox.shell", {"command": ["curl", "http://attacker.com"]})
    assert risk == RiskLevel.HIGH
    assert "network" in reason.lower()

    risk, reason = evaluate_risk("workspace.write_text", {"path": "/etc/shadow"})
    assert risk == RiskLevel.HIGH
    assert "sensitive" in reason.lower()

    # Safe in-workspace write is auto-approvable (canon P0): LOW, not MEDIUM.
    risk, reason = evaluate_risk("workspace.write_text", {"path": "output.txt"})
    assert risk == RiskLevel.LOW

    risk, reason = evaluate_risk("workspace.read_text", {"path": "output.txt"})
    assert risk == RiskLevel.LOW


def test_high_risk_action_triggers_waiting_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = f"sqlite:///{tmp_path/'db_high.sqlite'}"
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
            headers={**_auth(), "Idempotency-Key": "key-high-1"},
            json={
                "goal": "remove file",
                "path": "test.txt",
                "content": "",
                "tool_name": "sandbox.shell",
                "command": ["rm", "-f", "test.txt"],
            },
        )
        assert created.status_code == 201

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)

            result = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert result.status == TaskState.WAITING_APPROVAL.value
            assert len(result.approvals) == 1
            assert result.approvals[0].risk_level == RiskLevel.HIGH.value
            assert result.approvals[0].decision == "PENDING"


def test_approval_reject_blocks_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = f"sqlite:///{tmp_path/'db_reject.sqlite'}"
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
            headers={**_auth(), "Idempotency-Key": "key-reject-1"},
            # A write to a credentials file (*.env) is HIGH risk and still
            # requires approval; a plain in-workspace write now auto-approves.
            json={"goal": "write file", "path": "blocked.env", "content": "secret data"},
        )
        assert created.status_code == 201

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res1 = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert res1.status == TaskState.WAITING_APPROVAL.value
            job.status = "WAITING"
            session.commit()
            approval_id = res1.approvals[0].id

        rej_resp = gateway.post(
            f"/approvals/{approval_id}/decision",
            headers=_auth(),
            json={"approve": False},
        )
        assert rej_resp.status_code == 200
        assert rej_resp.json()["decision"] == "DENIED"

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res2 = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert res2.status == TaskState.POLICY_DENIED.value

        assert not (workspace / "blocked.env").exists()


def test_approval_approve_executes_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = f"sqlite:///{tmp_path/'db_approve.sqlite'}"
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
            headers={**_auth(), "Idempotency-Key": "key-approve-1"},
            # *.env write → HIGH risk, approval required (safe workspace write
            # is LOW/auto-approved and would never reach WAITING_APPROVAL).
            json={"goal": "write file", "path": "ok.env", "content": "approved content"},
        )
        assert created.status_code == 201

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res1 = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert res1.status == TaskState.WAITING_APPROVAL.value
            job.status = "WAITING"
            session.commit()
            approval_id = res1.approvals[0].id

        appr_resp = gateway.post(
            f"/approvals/{approval_id}/decision",
            headers=_auth(),
            json={"approve": True},
        )
        assert appr_resp.status_code == 200
        assert appr_resp.json()["decision"] == "APPROVED"

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res2 = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert res2.status == TaskState.DONE.value

        assert (workspace / "ok.env").read_text(encoding="utf-8") == "approved content"


def test_worker_restart_in_waiting_approval_does_not_break_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path/'db_restart.sqlite'}"
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
    gateway_app: FastAPI = create_gateway_app(settings)

    with TestClient(gateway_app) as gateway:
        created = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "key-restart-1"},
            # *.env write → HIGH risk, approval required (see above).
            json={"goal": "write file", "path": "restart.env", "content": "restart test"},
        )
        flow_id = str(created.json()["id"])

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res1 = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert res1.status == TaskState.WAITING_APPROVAL.value
            job.status = "WAITING"
            session.commit()

        with gateway_app.state.database.session_factory() as session:
            repo = TaskRepository(session)
            task = repo.get(flow_id)
            res2 = Orchestrator(session, tool, verifier).run(task, "worker-2-restarted")
            assert res2.status == TaskState.WAITING_APPROVAL.value
            assert res2.approvals[0].decision == "PENDING"

        approval_id = res1.approvals[0].id
        gateway.post(f"/approvals/{approval_id}/decision", headers=_auth(), json={"approve": True})

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-2-restarted", 30)
            assert job is not None
            task = TaskRepository(session).get(job.task_id)
            res3 = Orchestrator(session, tool, verifier).run(task, "worker-2-restarted")
            assert res3.status == TaskState.DONE.value

        assert (workspace / "restart.env").read_text(encoding="utf-8") == "restart test"


def test_approval_timeout_auto_reject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = f"sqlite:///{tmp_path/'db_timeout.sqlite'}"
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

    orig = get_confirmation_policy()
    set_confirmation_policy("ALWAYS", timeout_seconds=0.1)

    try:
        with TestClient(gateway_app) as gateway:
            created = gateway.post(
                "/flows",
                headers={**_auth(), "Idempotency-Key": "key-timeout-1"},
                # *.env write → HIGH risk, approval required (see above).
                json={"goal": "timeout flow", "path": "timeout.env", "content": "timeout data"},
            )
            flow_id = str(created.json()["id"])

            with gateway_app.state.database.session_factory() as session:
                queue = DurableQueue(session)
                job = queue.claim("worker-1", 30)
                assert job is not None
                task = TaskRepository(session).get(job.task_id)
                res1 = Orchestrator(session, tool, verifier).run(task, "worker-1")
                assert res1.status == TaskState.WAITING_APPROVAL.value

            time.sleep(0.25)

            with gateway_app.state.database.session_factory() as session:
                repo = TaskRepository(session)
                task = repo.get(flow_id)
                res2 = Orchestrator(session, tool, verifier).run(task, "worker-1")
                assert res2.status == TaskState.POLICY_DENIED.value
                assert res2.approvals[0].decision == "DENIED"
                assert res2.approvals[0].decided_by == "system_timeout"

            appr = res1.approvals[0]
            assert check_approval_timeout(appr, 0.1) is True
    finally:
        set_confirmation_policy(orig)
