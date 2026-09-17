from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.database import Database
from antigona.gateway.api import create_gateway_app
from antigona.repository import TaskRepository
from antigona.verifier_service import create_verifier_app
from antigona.worker.agent_core import AgentCoreConfig, create_worker_agent_core


def _events(caplog: Any, correlation_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "antigona"
        and json.loads(record.message).get("correlation_id") == correlation_id
    ]


def _assert_envelopes(events: list[dict[str, Any]], correlation_id: str) -> None:
    required = {
        "timestamp", "event", "service", "correlation_id",
        "task_id", "session_id", "step_id", "status",
    }
    assert events
    for item in events:
        assert required <= item.keys()
        assert item["timestamp"].endswith("Z")
        assert item["correlation_id"] == correlation_id


def test_gateway_worker_verifier_share_complete_correlation_trajectory(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'trajectory.sqlite'}"
    workspace = tmp_path / "workspace"
    correlation_id = "synthetic-correlation-trajectory"
    caplog.set_level(logging.INFO, logger="antigona")
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))

    gateway = create_gateway_app(
        Settings(database_url, workspace, {"synthetic-gateway-token": "synthetic-session"})
    )
    with TestClient(gateway) as client:
        response = client.post(
            "/flows",
            headers={
                "Authorization": "Bearer synthetic-gateway-token",
                "Idempotency-Key": "synthetic-idempotency",
                "X-Correlation-Id": correlation_id,
            },
            json={"goal": "write result", "path": "result.txt", "content": "expected result"},
        )
    assert response.status_code == 201
    task_id = response.json()["id"]

    database = Database(database_url)
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task = repository.get(task_id)
        core = create_worker_agent_core(
            AgentCoreConfig(workspace, tmp_path / "persistence", "synthetic-session"),
            repository,
            force_scripted=True,
        )
        core.run_turn(
            task,
            task.steps[0],
            json.dumps(
                {
                    "tool": "workspace.write_text",
                    "arguments": {"path": "result.txt", "content": "expected result"},
                }
            ),
            correlation_id,
        )

    seed_private_criteria(database_url, task_id)
    verifier = create_verifier_app(
        database_url,
        credential="synthetic-verifier-credential",
        judge=deterministic_test_judge(approved=True),
    )
    with TestClient(verifier) as client:
        response = client.post(
            "/verify",
            headers={"Authorization": "Bearer synthetic-verifier-credential"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
    assert response.json() == {"decision": "DONE"}

    trajectory: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != "antigona":
            continue
        payload = json.loads(record.message)
        if payload.get("correlation_id") == correlation_id:
            trajectory.append(payload)

    assert {item["service"] for item in trajectory} >= {"gateway", "worker", "verifier"}
    assert any(item["event"] == "gateway.flow_created" for item in trajectory)
    assert any(item["event"] == "worker.turn.completed" for item in trajectory)
    assert any(item["event"] == "verifier.completed" for item in trajectory)
    for item in trajectory:
        assert item["timestamp"].endswith("Z")
        assert item["correlation_id"] == correlation_id
        assert item["task_id"] == task_id
        assert "session_id" in item
        assert "step_id" in item
        assert "status" in item


def test_gateway_worker_and_verifier_failures_share_typed_envelope(
    tmp_path: Path, monkeypatch: Any, caplog: Any,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'failures.sqlite'}"
    workspace = tmp_path / "workspace"
    cid = "synthetic-correlation-failures"
    headers = {
        "Authorization": "Bearer synthetic-gateway-token",
        "X-Correlation-Id": cid,
    }
    caplog.set_level(logging.INFO, logger="antigona")
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
    gateway = create_gateway_app(
        Settings(database_url, workspace, {"synthetic-gateway-token": "synthetic-session"})
    )
    with TestClient(gateway) as client:
        denied = client.get("/flows/missing", headers={"X-Correlation-Id": cid})
        assert denied.status_code == 401
        failed_task_id = client.post(
            "/flows", headers={**headers, "Idempotency-Key": "worker-failure"},
            json={"goal": "fail", "path": "failed.txt", "content": "unused"},
        ).json()["id"]
        rejected_task_id = client.post(
            "/flows", headers={**headers, "Idempotency-Key": "verifier-rejection"},
            json={"goal": "reject", "path": "reject.txt", "content": "candidate"},
        ).json()["id"]

    database = Database(database_url)
    with database.session_factory() as session:
        repository = TaskRepository(session)
        failed = repository.get(failed_task_id)
        core = create_worker_agent_core(
            AgentCoreConfig(workspace, tmp_path / "persistence-failed", "synthetic-session"),
            repository, force_scripted=True,
        )
        with pytest.raises(KeyError):
            core.run_turn(failed, failed.steps[0], json.dumps({"tool": "missing", "arguments": {}}), cid)

    with database.session_factory() as session:
        repository = TaskRepository(session)
        candidate = repository.get(rejected_task_id)
        core = create_worker_agent_core(
            AgentCoreConfig(workspace, tmp_path / "persistence-rejected", "synthetic-session"),
            repository, force_scripted=True,
        )
        core.run_turn(
            candidate, candidate.steps[0],
            json.dumps({"tool": "workspace.write_text", "arguments": {"path": "reject.txt", "content": "candidate"}}),
            cid,
        )

    seed_private_criteria(database_url, rejected_task_id)
    verifier = create_verifier_app(
        database_url, credential="synthetic-verifier-credential",
        judge=deterministic_test_judge(approved=False),
    )
    with TestClient(verifier) as client:
        response = client.post(
            "/verify", headers={"Authorization": "Bearer synthetic-verifier-credential"},
            json={"task_id": rejected_task_id, "correlation_id": cid},
        )
    assert response.json() == {"decision": "REPLAN"}

    events = _events(caplog, cid)
    _assert_envelopes(events, cid)
    assert {"gateway.authentication_failed", "worker.turn.failed", "verifier.rejected"} <= {
        item["event"] for item in events
    }
