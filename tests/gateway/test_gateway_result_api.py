from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from antigona.config import Settings
from antigona.database import Database
from antigona.gateway.api import create_gateway_app
from antigona.models import (
    Approval,
    Artifact,
    DeliveryOutbox,
    FlowStep,
    QueueJob,
    StateTransition,
    TaskFlow,
    TaskState,
    utcnow,
)
from antigona.result_safety import MAX_RESULT_TEXT
from antigona.schemas import FlowResultView

TOKENS = {"alice-token": "alice", "bob-token": "bob"}
CID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[TestClient, Database, Path]:
    workspace = tmp_path / "workspace"
    settings = Settings(
        f"sqlite:///{tmp_path / 'gateway-result.db'}",
        workspace,
        TOKENS,
        "inprocess",
        test_mode=True,
    )
    app = create_gateway_app(settings)
    client = TestClient(app)
    client.__enter__()
    return client, app.state.database, workspace


def auth(token: str = "alice-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_flow(client: TestClient, key: str) -> dict[str, Any]:
    response = client.post(
        "/flows",
        headers={**auth(), "Idempotency-Key": key, "X-Correlation-Id": CID},
        json={"goal": "count bytes", "path": "reports/result.txt", "content": "value"},
    )
    assert response.status_code == 201
    return response.json()


def mark_terminal(
    database: Database,
    flow_id: str,
    status: TaskState,
    *,
    artifact_path: str | None = None,
    read_back: str | None = None,
    stdout_preview: str | None = None,
    verified: bool = True,
    reason: str | None = None,
) -> None:
    with database.session_factory() as session:
        task = session.get(TaskFlow, flow_id)
        assert task is not None
        step = task.steps[0]
        if stdout_preview is not None:
            step.output = {
                "ok": status is TaskState.DONE,
                "tool_result": {
                    "ok": status is TaskState.DONE,
                    "status": "completed" if status is TaskState.DONE else "failed",
                    "stdout_preview": stdout_preview,
                },
            }
        if artifact_path is not None:
            session.add(
                Artifact(
                    task_id=task.id,
                    step_id=step.id,
                    path=artifact_path,
                    sha256="a" * 64,
                    size=len((read_back or "").encode()),
                    verified=verified,
                    evidence={"read_back": read_back or "", "sha256": "a" * 64},
                )
            )
        old_status = task.status
        task.status = status.value
        task.revision += 1
        task.updated_at = utcnow()
        session.add(
            StateTransition(
                task_id=task.id,
                entity_id=task.id,
                entity_type="task",
                from_state=old_status,
                to_state=status.value,
                reason=reason or ("Verifier passed" if status is TaskState.DONE else "tool failed"),
                actor="verifier-service" if status is TaskState.DONE else "orchestrator",
                correlation_id=CID,
            )
        )
        session.commit()


def test_submit_result_is_explicitly_nonterminal(env: tuple[TestClient, Database, Path]) -> None:
    client, _, _ = env
    flow = create_flow(client, "nonterminal")

    response = client.get(f"/flows/{flow['id']}/result", headers=auth())

    assert response.status_code == 200
    result = FlowResultView.model_validate(response.json())
    assert result.status == "QUEUED"
    assert result.terminal is False
    assert result.success is False
    assert result.safe_result_text is None
    assert result.stdout_preview is None
    assert result.completed_at is None


def test_result_endpoint_owner_scope_matches_flow_access(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, _, _ = env
    flow = create_flow(client, "owner-scope")

    assert client.get(f"/flows/{flow['id']}/result").status_code == 401
    assert client.get(
        f"/flows/{flow['id']}/result", headers=auth("bob-token")
    ).status_code == 404
    assert client.get(f"/flows/{flow['id']}/result", headers=auth()).status_code == 200


def test_done_result_returns_only_verified_safe_bounded_content(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, workspace = env
    flow = create_flow(client, "safe-done")
    artifact_path = "reports/result.txt"
    (workspace / "reports").mkdir(parents=True)
    (workspace / artifact_path).write_text("safe", encoding="utf-8")
    oversized = "42 reports/input.txt\n" + ("x" * (MAX_RESULT_TEXT * 2))
    mark_terminal(
        database,
        flow["id"],
        TaskState.DONE,
        artifact_path=artifact_path,
        read_back=oversized,
        stdout_preview=oversized,
    )

    response = client.get(f"/flows/{flow['id']}/result", headers=auth())
    body = response.json()

    assert response.status_code == 200
    assert body["terminal"] is True
    assert body["success"] is True
    assert len(body["safe_result_text"]) <= MAX_RESULT_TEXT
    assert len(body["stdout_preview"]) <= MAX_RESULT_TEXT
    assert body["artifacts"] == [
        {
            "path": artifact_path,
            "sha256": "a" * 64,
            "size": len(oversized.encode()),
            "verified": True,
        }
    ]
    # The generic flow endpoint no longer exposes raw verifier evidence.
    flow_body = client.get(f"/flows/{flow['id']}", headers=auth()).json()
    assert "evidence" not in flow_body["artifacts"][0]


def test_result_endpoint_redacts_secret_looking_stdout(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, workspace = env
    flow = create_flow(client, "redacted")
    artifact_path = "reports/redacted.txt"
    (workspace / "reports").mkdir(parents=True)
    (workspace / artifact_path).write_text("safe", encoding="utf-8")
    raw = "Bearer synthetic-token-value\nAPI_KEY=synthetic-api-key"
    mark_terminal(
        database,
        flow["id"],
        TaskState.DONE,
        artifact_path=artifact_path,
        read_back=raw,
        stdout_preview=raw,
    )

    serialized = client.get(f"/flows/{flow['id']}/result", headers=auth()).text

    assert "synthetic-token-value" not in serialized
    assert "synthetic-api-key" not in serialized
    assert "[REDACTED]" in serialized


def test_done_without_verified_artifact_is_not_a_success(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, _ = env
    flow = create_flow(client, "done-without-proof")
    mark_terminal(
        database,
        flow["id"],
        TaskState.DONE,
        stdout_preview="unverified output",
    )

    result = client.get(f"/flows/{flow['id']}/result", headers=auth()).json()

    assert result["terminal"] is True
    assert result["success"] is False
    assert result["artifacts"] == []
    assert result["safe_result_text"] is None
    assert result["stdout_preview"] is None
    assert result["failure_reason"] == "verified result unavailable"


def test_sensitive_artifact_and_command_result_are_omitted_and_not_successful(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, _ = env
    flow = create_flow(client, "sensitive")
    with database.session_factory() as session:
        task = session.get(TaskFlow, flow["id"])
        assert task is not None
        task.target_path = ".env"
        task.tool_name = "sandbox.shell"
        task.tool_arguments = {"command": ["cat", ".env"]}
        session.commit()
    raw = "PASSWORD=synthetic-password"
    mark_terminal(
        database,
        flow["id"],
        TaskState.DONE,
        artifact_path=".env",
        read_back=raw,
        stdout_preview=raw,
    )

    response = client.get(f"/flows/{flow['id']}/result", headers=auth())
    body = response.json()

    assert body["terminal"] is True
    assert body["success"] is False
    assert body["artifacts"] == []
    assert body["safe_result_text"] is None
    assert body["stdout_preview"] is None
    assert body["failure_reason"] == "result withheld by safety policy"
    assert "synthetic-password" not in response.text


def test_failed_flow_returns_safe_reason_without_success(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, _ = env
    flow = create_flow(client, "failed")
    mark_terminal(
        database,
        flow["id"],
        TaskState.FAILED,
        stdout_preview="Traceback: synthetic diagnostic",
        reason="tool execution failed",
    )

    result = client.get(f"/flows/{flow['id']}/result", headers=auth()).json()

    assert result["status"] == "FAILED"
    assert result["terminal"] is True
    assert result["success"] is False
    assert result["failure_reason"] == "tool execution failed"
    assert result["safe_result_text"] is None
    assert result["stdout_preview"] is None


def test_result_endpoint_is_read_only_and_cannot_bypass_verifier(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, _ = env
    flow = create_flow(client, "read-only")
    with database.session_factory() as session:
        before = session.get(TaskFlow, flow["id"])
        assert before is not None
        before_state = before.status
        before_revision = before.revision
        before_transitions = len(before.transitions)

    response = client.get(f"/flows/{flow['id']}/result", headers=auth())
    assert response.status_code == 200

    with database.session_factory() as session:
        after = session.get(TaskFlow, flow["id"])
        assert after is not None
        assert after.status == before_state == "QUEUED"
        assert after.revision == before_revision
        assert len(after.transitions) == before_transitions
        assert not any(t.to_state == "DONE" for t in after.transitions)


@pytest.mark.parametrize(
    ("payload", "marker"),
    [
        (
            {
                "goal": "write report",
                "path": ".env/PRECOMMIT_HTTP_PATH_MARKER",
                "content": "safe payload",
            },
            "PRECOMMIT_HTTP_PATH_MARKER",
        ),
        (
            {
                "goal": "write report",
                "path": "reports/out.txt",
                "content": "API_KEY=PRECOMMIT_HTTP_CONTENT_MARKER",
            },
            "PRECOMMIT_HTTP_CONTENT_MARKER",
        ),
        (
            {
                "goal": "Authorization: Bearer PRECOMMIT_HTTP_GOAL_MARKER",
                "path": "reports/out.txt",
                "content": "safe payload",
            },
            "PRECOMMIT_HTTP_GOAL_MARKER",
        ),
        (
            {
                "goal": "run command",
                "path": "reports/out.txt",
                "content": "safe payload",
                "tool_name": "sandbox.shell",
                "command": ["printenv", "PRECOMMIT_HTTP_COMMAND_MARKER"],
            },
            "PRECOMMIT_HTTP_COMMAND_MARKER",
        ),
    ],
    ids=("path", "content", "goal", "command"),
)
def test_sensitive_post_is_safe_4xx_with_zero_persistence_and_no_raw_log(
    env: tuple[TestClient, Database, Path],
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, Any],
    marker: str,
) -> None:
    client, database, _ = env

    with caplog.at_level(logging.INFO, logger="antigona"):
        response = client.post(
            "/flows",
            headers={
                **auth(),
                "Idempotency-Key": f"reject-{marker}",
                "X-Correlation-Id": CID,
            },
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "task input rejected by safety policy"}
    assert marker not in response.text
    assert marker not in caplog.text

    with database.session_factory() as session:
        for model in (
            TaskFlow,
            FlowStep,
            Approval,
            StateTransition,
            DeliveryOutbox,
            QueueJob,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_generic_flow_and_public_replay_whitelist_legacy_nested_values(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, database, _ = env
    flow = create_flow(client, "legacy-public-projection")
    marker = "P0_PUBLIC_SECRET_MARKER_92A7"

    with database.session_factory() as session:
        task = session.get(TaskFlow, flow["id"])
        assert task is not None
        step = task.steps[0]
        task.goal = f"API_KEY={marker}"
        task.target_path = f".env/{marker}"
        task.content = marker
        task.tool_name = "sandbox.shell"
        task.tool_arguments = {
            "command": ["printf", marker],
            "headers": {"Authorization": marker},
        }
        step.title = f"API_KEY={marker}"
        step.input = {
            "command": ["printf", marker],
            "content": marker,
            "headers": {"Authorization": marker},
        }
        step.output = {
            "ok": False,
            "stderr": marker,
            "traceback": marker,
            "evidence": {"read_back": marker},
            "tool_result": {
                "ok": False,
                "status": "failed",
                "stderr": marker,
                "stdout_preview": marker,
                "evidence": {"read_back": marker},
            },
        }
        approval = Approval(
            task_id=task.id,
            tool_name="sandbox.shell",
            arguments={
                "command": ["printf", marker],
                "headers": {"Authorization": marker},
            },
            risk_level="HIGH",
            reason=f"RuntimeError: {marker}",
            decision="PENDING",
        )
        session.add(approval)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=step.id,
                path="reports/legacy.txt",
                sha256="b" * 64,
                size=1,
                verified=False,
                evidence={
                    "read_back": marker,
                    "nested": {"stderr": marker, "credentials": marker},
                },
            )
        )
        session.add(
            StateTransition(
                task_id=task.id,
                entity_id=task.id,
                entity_type="task",
                from_state=task.status,
                to_state=task.status,
                reason=f"RuntimeError: {marker}",
                actor="legacy-worker",
                correlation_id=CID,
            )
        )
        session.commit()
        approval_id = approval.id

    responses = [
        client.get(f"/flows/{flow['id']}", headers=auth()),
        client.get(f"/flows/{flow['id']}/replay", headers=auth()),
        client.get(f"/flows/{flow['id']}/replay/timeline", headers=auth()),
        client.get(f"/approvals/{approval_id}", headers=auth()),
    ]
    assert all(response.status_code == 200 for response in responses)

    serialized = json.dumps([response.json() for response in responses], sort_keys=True)
    assert marker not in serialized
    for forbidden_key in (
        "command",
        "content",
        "headers",
        "credentials",
        "stderr",
        "traceback",
        "evidence",
        "read_back",
        "stdout_preview",
    ):
        assert f'"{forbidden_key}"' not in serialized

    flow_body = responses[0].json()
    replay_body = responses[1].json()
    assert flow_body["status"] == replay_body["status"] == "QUEUED"
    assert flow_body["steps"][0]["index"] == replay_body["steps"][0]["index"] == 0
    assert flow_body["steps"][0]["input"].keys() == {"arguments_sha256"}
    assert replay_body["steps"][0]["input"].keys() == {"arguments_sha256"}
    assert flow_body["steps"][0]["output"] == {
        "ok": False,
        "tool_result": {"ok": False, "status": "failed"},
    }
    assert replay_body["steps"][0]["output"] == flow_body["steps"][0]["output"]


def test_safe_post_get_and_replay_keep_status_index_title_and_result_metadata(
    env: tuple[TestClient, Database, Path],
) -> None:
    client, _, _ = env
    created = create_flow(client, "safe-public-projection")

    flow = client.get(f"/flows/{created['id']}", headers=auth()).json()
    replay = client.get(f"/flows/{created['id']}/replay", headers=auth()).json()

    for body in (created, flow):
        assert body["status"] == "QUEUED"
        assert body["steps"][0]["index"] == 0
        assert body["steps"][0]["title"] == "Execute workspace.write_text and verify"
        assert body["steps"][0]["input"].keys() == {
            "tool_name",
            "arguments_sha256",
        }
    assert replay["status"] == "QUEUED"
    assert replay["steps"][0]["index"] == 0
    assert replay["steps"][0]["title"] == "Execute workspace.write_text and verify"
    assert replay["steps"][0]["input"].keys() == {
        "tool_name",
        "arguments_sha256",
    }
