from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect
from sqlalchemy import select

from antigona.config import Settings
from antigona.database import Database
from antigona.gateway.api import create_gateway_app
from antigona.models import QueueJob, StateTransition, TaskFlow, TaskState
from antigona.repository import InvalidTransition, TaskRepository

TOKENS = {"alice-token": "alice", "bob-token": "bob"}
CID = "11111111-2222-3333-4444-555555555555"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        f"sqlite:///{tmp_path/'db.sqlite'}", tmp_path / "workspace", TOKENS,
        "inprocess", test_mode=True,
    )


@pytest.fixture()
def env(tmp_path: Path) -> tuple[TestClient, Database]:
    settings = make_settings(tmp_path)
    app = create_gateway_app(settings)
    client = TestClient(app)
    client.__enter__()
    return client, app.state.database


def auth(token: str = "alice-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_flow(client: TestClient, key: str = "k", cid: str = CID) -> Any:
    return client.post(
        "/flows",
        headers={**auth(), "Idempotency-Key": key, "X-Correlation-Id": cid},
        json={"goal": "g", "path": "x.txt", "content": "value"},
    )


def test_create_flow_enqueues_and_propagates_correlation_id(env: tuple[TestClient, Database]) -> None:
    client, database = env
    response = create_flow(client)
    assert response.status_code == 201
    assert response.headers["X-Correlation-Id"] == CID
    flow = response.json()
    assert flow["status"] == "QUEUED"
    with database.session_factory() as session:
        assert session.scalar(select(TaskFlow).where(TaskFlow.id == flow["id"])) is not None
        job = session.scalar(select(QueueJob).where(QueueJob.task_id == flow["id"]))
        assert job is not None and job.correlation_id == CID
        transitions = session.scalars(
            select(StateTransition).where(StateTransition.task_id == flow["id"])
        ).all()
        assert transitions and all(t.correlation_id == CID for t in transitions)


def test_get_flow_and_auth_isolation(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    flow = create_flow(client).json()
    assert client.get(f"/flows/{flow['id']}").status_code == 401
    assert client.get(f"/flows/{flow['id']}", headers=auth("bob-token")).status_code == 404
    ok = client.get(f"/flows/{flow['id']}", headers=auth())
    assert ok.status_code == 200 and ok.json()["id"] == flow["id"]


def test_cancel_is_sticky(env: tuple[TestClient, Database]) -> None:
    client, database = env
    flow = create_flow(client, "cancel-key").json()
    cancelled = client.post(f"/flows/{flow['id']}/cancel", headers=auth()).json()
    assert cancelled["status"] == "CANCELLED" and cancelled["cancellation_requested"] is True
    # sticky: repository refuses to move a cancelled flow anywhere else
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task = repository.get(flow["id"])
        with pytest.raises(InvalidTransition):
            repository.transition(task, TaskState.PLANNING, "resume attempt", "gateway")


def test_websocket_events_carry_correlation_id(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    flow = create_flow(client, "ws-key").json()
    client.post(
        f"/flows/{flow['id']}/cancel",
        headers={**auth(), "X-Correlation-Id": CID},
    )
    seen: list[dict[str, Any]] = []
    with client.websocket_connect(
        f"/flows/{flow['id']}/progress?token=alice-token&correlation_id={CID}"
    ) as ws:
        while True:
            message = ws.receive_json()
            if message["type"] == "end":
                assert message["correlation_id"] == CID
                break
            seen.append(message)
    assert seen
    assert all(event["correlation_id"] == CID for event in seen)
    assert any(event["to_state"] == "CANCELLED" for event in seen)


def test_websocket_rejects_bad_token(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    flow = create_flow(client, "ws-auth").json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/flows/{flow['id']}/progress?token=wrong") as ws:
            ws.receive_json()


def test_approval_decision_endpoint(env: tuple[TestClient, Database]) -> None:
    client, database = env
    # .env -> HIGH risk, approval required (canon P0: create_flow()'s default
    # "x.txt" is a plain in-workspace write -> LOW -> auto-APPROVED, which would
    # make request_approval() below return an already-decided approval and the
    # /decision POST this test exercises would 409 immediately -- see
    # tests/unit/test_failure_b_approval_resume.py for the same pattern).
    flow = client.post(
        "/flows",
        headers={**auth(), "Idempotency-Key": "appr-key", "X-Correlation-Id": CID},
        json={"goal": "g", "path": "appr_result.env", "content": "value"},
    ).json()
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task = repository.get(flow["id"])
        approval = repository.request_approval(task)
        session.commit()
        approval_id = approval.id
    decided = client.post(
        f"/approvals/{approval_id}/decision", headers=auth(), json={"approve": True}
    )
    assert decided.status_code == 200 and decided.json()["decision"] == "APPROVED"
    again = client.post(
        f"/approvals/{approval_id}/decision", headers=auth(), json={"approve": False}
    )
    assert again.status_code == 409
    assert client.post(
        "/approvals/does-not-exist/decision", headers=auth(), json={"approve": True}
    ).status_code == 404


def test_gateway_has_no_done_capability(env: tuple[TestClient, Database]) -> None:
    client, database = env
    flow = create_flow(client, "done-key").json()
    # 1. No route in the OpenAPI schema finalizes a flow.
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("done" in p.lower() or "finalize" in p.lower() or "complete" in p.lower() for p in paths)
    # 2. The repository the gateway uses refuses DONE outright.
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task = repository.get(flow["id"])
        with pytest.raises(InvalidTransition):
            repository.transition(task, TaskState.DONE, "attempt", "gateway")


def test_structured_logs_include_correlation_id(
    env: tuple[TestClient, Database], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = env
    with caplog.at_level(logging.INFO, logger="antigona"):
        create_flow(client, "log-key")
    records = [json.loads(r.message) for r in caplog.records if r.name == "antigona"]
    created = [r for r in records if r.get("event") == "gateway.flow_created"]
    assert created and created[0]["correlation_id"] == CID


def test_missing_correlation_header_generates_one(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    response = client.post(
        "/flows",
        headers={**auth(), "Idempotency-Key": "gen-key"},
        json={"goal": "g", "path": "y.txt", "content": "v"},
    )
    assert response.status_code == 201
    assert len(response.headers["X-Correlation-Id"]) == 36


def test_gateway_failure_outcomes_are_structured_and_sanitized(
    env: tuple[TestClient, Database], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database = env
    caplog.set_level(logging.INFO, logger="antigona")
    assert client.get("/flows/missing", headers={"X-Correlation-Id": CID}).status_code == 401
    assert client.get(
        "/flows/missing", headers={**auth(), "X-Correlation-Id": CID}
    ).status_code == 404
    first = create_flow(client, "conflict-key")
    assert first.status_code == 201
    conflict = client.post(
        "/flows",
        headers={**auth(), "Idempotency-Key": "conflict-key", "X-Correlation-Id": CID},
        json={"goal": "different", "path": "x.txt", "content": "value"},
    )
    assert conflict.status_code == 409
    assert client.post(
        "/approvals/missing/decision",
        headers={**auth(), "X-Correlation-Id": CID}, json={"approve": True},
    ).status_code == 404

    flow_id = first.json()["id"]
    original_cancel = TaskRepository.cancel

    def reject_cancel(self: TaskRepository, task: TaskFlow, correlation_id: str | None = None) -> TaskFlow:
        raise InvalidTransition("synthetic internal detail must not be logged")

    monkeypatch.setattr(TaskRepository, "cancel", reject_cancel)
    assert client.post(
        f"/flows/{flow_id}/cancel", headers={**auth(), "X-Correlation-Id": CID}
    ).status_code == 409
    monkeypatch.setattr(TaskRepository, "cancel", original_cancel)

    records = [json.loads(record.message) for record in caplog.records if record.name == "antigona"]
    names = {record["event"] for record in records}
    assert {
        "gateway.authentication_failed", "gateway.flow_not_found",
        "gateway.idempotency_conflict", "gateway.approval_not_found",
        "gateway.cancellation_failed",
    } <= names
    failures = [record for record in records if record["event"] in names and record["status"] in {"401", "404", "409"}]
    assert failures
    assert all(
        {"timestamp", "service", "event", "correlation_id", "task_id", "session_id", "step_id", "status", "reason"} <= record.keys()
        for record in failures
    )
    assert "synthetic internal detail" not in json.dumps(failures)


def test_steer_flow_not_found(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    response = client.post(
        "/flows/nonexistent-id/steer",
        headers=auth(),
        json={"message": "change strategy"},
    )
    assert response.status_code == 404


def test_steer_flow_invalid_status(env: tuple[TestClient, Database]) -> None:
    client, _ = env
    flow = create_flow(client, "steer-queued-key").json()
    # Initial status is QUEUED (not WAITING_APPROVAL or RUNNING)
    response = client.post(
        f"/flows/{flow['id']}/steer",
        headers=auth(),
        json={"message": "change strategy"},
    )
    assert response.status_code == 400


def test_steer_flow_valid_status(env: tuple[TestClient, Database]) -> None:
    client, database = env
    flow = create_flow(client, "steer-valid-key").json()
    # Move flow to RUNNING state in DB
    with database.session_factory() as session:
        tf = session.scalar(select(TaskFlow).where(TaskFlow.id == flow["id"]))
        assert tf is not None
        tf.status = "RUNNING"
        session.commit()

    response = client.post(
        f"/flows/{flow['id']}/steer",
        headers=auth(),
        json={"message": "use local python python3.11"},
    )
    assert response.status_code == 200
    res = response.json()
    assert res["id"] == flow["id"]

    # Verify steer message recorded in DB
    with database.session_factory() as session:
        tf = session.scalar(select(TaskFlow).where(TaskFlow.id == flow["id"]))
        assert tf is not None
        steer_msgs = tf.tool_arguments.get("steer_messages", [])
        assert "use local python python3.11" in steer_msgs

