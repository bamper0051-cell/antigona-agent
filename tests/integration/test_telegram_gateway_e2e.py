from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.delivery import DeliveryWorker, TelegramAdapter
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.gateway.api import create_gateway_app
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

    def request_unauthenticated(self, task_id: str, correlation_id: str) -> int:
        response = self._client.post(
            "/verify",
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        return int(response.status_code)


def _auth(token: str = "gateway-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_telegram_gateway_worker_verifier_delivery_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'db.sqlite'}"
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

    sent: list[dict[str, str]] = []

    def fake_open(request: Any, timeout: int = 0, **_kwargs: Any) -> Any:
        del timeout
        sent.append(
            {
                "url": str(request.full_url),
                "body": request.data.decode(),
                "idempotency": str(dict(request.headers).get("Idempotency-Key", "")),
            }
        )

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

            def close(self) -> None:
                return None

        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    adapter = TelegramAdapter("bot-token", "chat-1")
    gateway_app: FastAPI = create_gateway_app(settings)

    with TestClient(gateway_app) as gateway:
        created = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "tg-e2e-key"},
            # .env -> HIGH risk, approval required (canon P0: a plain in-workspace
            # write is LOW/auto-approved and would skip the WAITING_APPROVAL leg this
            # test exercises -- see tests/unit/test_failure_b_approval_resume.py).
            json={"goal": "create file", "path": "x.env", "content": "from telegram"},
        )
        assert created.status_code == 201
        flow_id = str(created.json()["id"])

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            first_job = queue.claim("worker-1", 30)
            assert first_job is not None
            task = TaskRepository(session).get(first_job.task_id)
            first_result = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert first_result.status == "WAITING_APPROVAL"
            first_job.status = "WAITING"
            session.commit()
            approval_id = first_result.approvals[0].id

        approval = gateway.post(
            f"/approvals/{approval_id}/decision",
            headers=_auth(),
            json={"approve": True},
        )
        assert approval.status_code == 200

        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            second_job = queue.claim("worker-1", 30)
            assert second_job is not None
            task = TaskRepository(session).get(second_job.task_id)
            second_result = Orchestrator(session, tool, verifier).run(task, "worker-1")
            assert second_result.status == "DONE"
            queue.finish(second_job)

        delivery = DeliveryWorker(gateway_app.state.database.session_factory(), adapter)
        while delivery.dispatch_one():
            pass

        with gateway_app.state.database.session_factory() as session:
            done = TaskRepository(session).get(flow_id)
            assert done.status == "DONE"

    assert (workspace / "x.env").read_text(encoding="utf-8") == "from telegram"
    assert sent
    assert all(
        call["url"].startswith("https://api.telegram.org/botbot-token/sendMessage") for call in sent
    )
    parsed_payloads = [json.loads(call["body"]) for call in sent]
    assert any(payload["chat_id"] == "chat-1" for payload in parsed_payloads)
    assert any("DONE" in payload["text"] for payload in parsed_payloads)


def test_telegram_gateway_worker_sdk_verifier_delivery_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from antigona.worker.agent_core import AgentCoreConfig, create_worker_agent_core

    database_url = f"sqlite:///{tmp_path / 'sdk_db.sqlite'}"
    workspace = tmp_path / "sdk_workspace"
    persistence = tmp_path / "sdk_persistence"
    settings = Settings(
        database_url=database_url,
        workspace=workspace,
        dev_tokens={"gateway-token": "owner-1"},
        sandbox_backend="inprocess",
        test_mode=True,
    )
    verifier = VerifierHarness(database_url, workspace, monkeypatch)

    sent: list[dict[str, str]] = []

    def fake_open(request: Any, timeout: int = 0, **_kwargs: Any) -> Any:
        del timeout
        sent.append(
            {
                "url": str(request.full_url),
                "body": request.data.decode(),
                "idempotency": str(dict(request.headers).get("Idempotency-Key", "")),
            }
        )

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

            def close(self) -> None:
                return None

        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    adapter = TelegramAdapter("bot-sdk-token", "chat-sdk-1")
    gateway_app: FastAPI = create_gateway_app(settings)

    with TestClient(gateway_app) as gateway:
        # Step 1: Telegram user sends request to Gateway -> creates flow & enqueues to queue_jobs
        created = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "tg-sdk-key"},
            json={"goal": "create file X", "path": "X.txt", "content": "from telegram via sdk"},
        )
        assert created.status_code == 201
        flow_id = str(created.json()["id"])

        # Step 2: Worker claims job from queue_jobs
        with gateway_app.state.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-sdk-1", 30)
            assert job is not None
            assert job.task_id == flow_id
            assert job.status == "RUNNING"

            repo = TaskRepository(session)
            task = repo.get(job.task_id)

            # Step 3: Worker uses WorkerAgentCore (Worker SDK) to execute turn and write file X
            core = create_worker_agent_core(
                AgentCoreConfig(
                    workspace=workspace,
                    persistence_dir=persistence,
                    conversation_id=task.id,
                ),
                repo,
                force_scripted=True,
            )
            prompt = json.dumps(
                {
                    "tool": "workspace.write_text",
                    "arguments": {"path": task.target_path, "content": task.content},
                }
            )
            core.run_turn(
                task=task, step=task.steps[0], prompt=prompt, correlation_id=job.correlation_id
            )

            # Assert task transitioned to VERIFYING and file X exists in workspace
            refreshed = repo.get(flow_id)
            assert refreshed.status == "VERIFYING"
            assert (workspace / "X.txt").read_text(encoding="utf-8") == "from telegram via sdk"

            # Step 4: Verify unauthenticated request to Verifier fails with HTTP 401
            unauth_status = verifier.request_unauthenticated(flow_id, job.correlation_id)
            assert unauth_status == 401

            # Step 5: Worker invokes Verifier endpoint with bearer token -> updates task to DONE
            decision = verifier.request_verification(flow_id, job.correlation_id)
            assert decision == "DONE"

            done_task = repo.get(flow_id)
            assert done_task.status == "DONE"
            queue.finish(job)

        # Step 6: DeliveryWorker dispatches outbox notifications via TelegramAdapter (send-only)
        delivery = DeliveryWorker(gateway_app.state.database.session_factory(), adapter)
        dispatched_count = 0
        while delivery.dispatch_one():
            dispatched_count += 1
        assert dispatched_count > 0

    # Step 7: Verify end-to-end outcome & delivery assertions
    assert (workspace / "X.txt").read_text(encoding="utf-8") == "from telegram via sdk"
    assert sent
    assert all(
        call["url"].startswith("https://api.telegram.org/botbot-sdk-token/sendMessage")
        for call in sent
    )
    parsed_payloads = [json.loads(call["body"]) for call in sent]
    assert any(payload["chat_id"] == "chat-sdk-1" for payload in parsed_payloads)
    assert any("DONE" in payload["text"] for payload in parsed_payloads)
