"""Phase L10-R canonical-path regression tests.

Exercises the exact integration path the ``antigona chat`` CLI uses in
production: POST /api/v1/dialogue/turn on the real Gateway app (AntigonaBrain +
TaskSubmissionService + DurableQueue + Orchestrator), not brain.process()
called in isolation. A prior local-only reproduction (calling
AntigonaBrain.process() directly without an owner_id in context) missed a real
regression: every real Gateway request carries a resolved owner_id, which
routed bare pwd/ls through the PIN-gated owner-shell path and always denied
them with "Owner shell denied". These tests pin the fix at the same layer
boundary a real CLI session crosses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge

from antigona.config import Settings
from antigona.contracts import ToolResult
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.gateway.api import create_gateway_app
from antigona.models import TaskState
from antigona.orchestrator import Orchestrator
from antigona.queue import DurableQueue
from antigona.repository import TaskRepository
from antigona.verifier_service import create_verifier_app

TOKENS = {"gateway-token": "owner-1"}


class RecordingShell:
    """Fake DockerShellTool double: exercises the same ToolResult contract as
    the real sandbox shell tool without depending on a docker daemon being
    reachable in the test environment."""

    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls = 0

    def execute(self, _arguments: object) -> ToolResult:
        self.calls += 1
        return self.result


def _auth(token: str = "gateway-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Settings]:
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path/'db.sqlite'}",
        workspace=tmp_path / "workspace",
        dev_tokens=TOKENS,
        sandbox_backend="inprocess",
        test_mode=True,
    )
    app = create_gateway_app(settings)
    client = TestClient(app)
    client.__enter__()
    return client, settings


def test_canonical_dialogue_turn_hi_pwd_ls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariants 1, 3, 4, 5: hi/pwd/ls through the real Gateway dialogue-turn
    endpoint — the exact call the CLI makes — behave correctly end to end."""
    client, _ = _make_app(tmp_path, monkeypatch)
    sid = "l10r-canonical-session"

    r = client.post(
        "/api/v1/dialogue/turn",
        json={"text": "hi", "session_id": sid, "channel": "cli", "user_id": "u1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["response_type"] == "conversation"
    assert data["flow_id"] is None

    r = client.post(
        "/api/v1/dialogue/turn",
        json={"text": "pwd", "session_id": sid, "channel": "cli", "user_id": "u1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["response_type"] == "conversation"
    assert data["flow_id"] is None
    assert "Owner shell denied" not in data["reply"]
    assert "/" in data["reply"]

    r = client.post(
        "/api/v1/dialogue/turn",
        json={"text": "ls", "session_id": sid, "channel": "cli", "user_id": "u1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["response_type"] == "conversation"
    assert data["flow_id"] is None
    assert "Owner shell denied" not in data["reply"]


def test_shell_task_approval_resume_and_truthful_failure_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariants 7, 10, 11, 18: an ambiguous free-text shell request ("install
    git") creates a task requiring approval; approving it resumes execution
    exactly once (APPROVED != COMPLETED — re-deciding the same approval is
    rejected, not silently re-executed); the terminal failure reason is the
    real captured detail, not the generic 'tool execution failed'."""
    client, settings = _make_app(tmp_path, monkeypatch)
    sid = "l10r-install-session"

    r = client.post(
        "/api/v1/dialogue/turn",
        json={"text": "install git", "session_id": sid, "channel": "cli", "user_id": "u1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["response_type"] == "task_accepted"
    assert data["requires_approval"] is True
    flow_id = data["flow_id"]
    assert flow_id

    tool = WorkspaceFileTool(InProcessTestBackend(settings.workspace, test_mode=True))
    verifier_client = TestClient(
        create_verifier_app(settings.database_url, "verifier-secret", deterministic_test_judge())
    )
    verifier_client.__enter__()

    class Verifier:
        def request_verification(self, task_id: str, correlation_id: str) -> str:
            resp = verifier_client.post(
                "/verify",
                headers={"Authorization": "Bearer verifier-secret"},
                json={"task_id": task_id, "correlation_id": correlation_id},
            )
            resp.raise_for_status()
            return str(resp.json()["decision"])

    shell = RecordingShell(
        ToolResult(
            False,
            "failed",
            error=(
                "tool exited non-zero (exit code 1): install: missing destination "
                "file operand after 'git'"
            ),
        )
    )

    app = client.app
    with app.state.database.session_factory() as session:  # type: ignore[attr-defined]
        queue = DurableQueue(session)
        job = queue.claim("worker-1", 30)
        assert job is not None
        task = TaskRepository(session).get(job.task_id)
        result = Orchestrator(session, tool, Verifier(), shell_tool=shell).run(task, "worker-1")
        assert result.status == TaskState.WAITING_APPROVAL.value
        job.status = "WAITING"
        session.commit()
        approval_id = result.approvals[0].id

    # Approve once.
    decision_resp = client.post(
        f"/approvals/{approval_id}/decision",
        headers=_auth(),
        json={"approve": True},
    )
    assert decision_resp.status_code == 200
    assert decision_resp.json()["decision"] == "APPROVED"

    # Re-deciding the same approval must be rejected, not silently accepted —
    # this is the APPROVED != COMPLETED / no-duplicate-execution invariant.
    duplicate_resp = client.post(
        f"/approvals/{approval_id}/decision",
        headers=_auth(),
        json={"approve": True},
    )
    assert duplicate_resp.status_code == 409

    # Worker resumes the suspended action exactly once.
    with app.state.database.session_factory() as session:  # type: ignore[attr-defined]
        queue = DurableQueue(session)
        job = queue.claim("worker-1", 30)
        assert job is not None
        task = TaskRepository(session).get(job.task_id)
        result = Orchestrator(session, tool, Verifier(), shell_tool=shell).run(task, "worker-1")
        assert result.status == TaskState.FAILED.value
        queue.finish(job)

    assert shell.calls == 1, "shell command must execute exactly once after approval"

    with app.state.database.session_factory() as session:  # type: ignore[attr-defined]
        task = TaskRepository(session).get(flow_id)
        assert task.status == TaskState.FAILED.value
        failure_reason = task.steps[0].output.get("tool_result", {}).get("failure_reason", "")
        assert failure_reason != "tool execution failed"
        assert "missing destination file operand" in failure_reason


def test_unknown_approval_id_returns_controlled_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 11 (section 15): an unknown approval ID is a controlled 404,
    never a model-dialogue response."""
    client, _ = _make_app(tmp_path, monkeypatch)
    resp = client.post(
        "/approvals/does-not-exist/decision",
        headers=_auth(),
        json={"approve": True},
    )
    assert resp.status_code == 404
