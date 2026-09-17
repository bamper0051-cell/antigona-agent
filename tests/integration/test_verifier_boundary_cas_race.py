"""Multi-process CAS race on the worker/verifier boundary (P4.3).

Gateway, worker and verifier each get their **own** :class:`Database` here, so each
holds a distinct engine, connection, transaction and ORM identity map -- exactly the
isolation three OS processes have against one shared durable layer. Tests that drive
all three roles through a single shared session cannot expose this race: the shared
identity map hides the fact that the verifier already wrote the terminal transition
in its own connection before answering the worker's HTTP call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verifier_fakes import deterministic_test_judge, seed_private_criteria

from antigona.config import Settings
from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.gateway.api import create_gateway_app
from antigona.models import QueueJob, StateTransition, TaskState
from antigona.orchestrator import Orchestrator
from antigona.queue import DurableQueue
from antigona.repository import TaskRepository
from antigona.verifier_service import create_verifier_app


class VerifierProcess:
    """The verifier service on its own connection, reached over HTTP like in production."""

    def __init__(
        self, database_url: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
        self._client = TestClient(
            create_verifier_app(database_url, "secret", deterministic_test_judge())
        )
        self._client.__enter__()

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        response = self._client.post(
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": correlation_id},
        )
        response.raise_for_status()
        return str(response.json()["decision"])


@dataclass
class WorkerTurn:
    """What a single worker loop iteration observed, mirroring ``worker.main``."""

    status: str
    job_status: str
    job_error: str | None
    error: str | None


class WorkerProcess:
    """One worker loop iteration against a private connection to the shared file DB."""

    def __init__(self, database_url: str, workspace: Path, verifier: VerifierProcess) -> None:
        self.database = Database(database_url)
        self.tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True))
        self.verifier = verifier

    def turn(self) -> WorkerTurn:
        with self.database.session_factory() as session:
            queue = DurableQueue(session)
            job = queue.claim("worker-1", 30)
            assert job is not None, "worker found no claimable job"
            task = TaskRepository(session).get(job.task_id)
            error: str | None = None
            try:
                result = Orchestrator(session, self.tool, self.verifier).run(task, "worker-1")
                status = result.status
                if TaskState(status) is TaskState.WAITING_APPROVAL:
                    job.status = "WAITING"
                    session.commit()
                else:
                    queue.finish(job)
            except Exception as exc:  # mirrors worker.main's failure handling
                error = f"{type(exc).__name__}: {exc}"
                job.status = "FAILED"
                job.last_error = str(exc)
                session.commit()
                status = TaskRepository(session).get(job.task_id).status
            return WorkerTurn(status, job.status, job.last_error, error)


def _auth(token: str = "gateway-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, TestClient, WorkerProcess]:
    database_url = f"sqlite:///{tmp_path/'cas_race.sqlite'}"
    workspace = tmp_path / "workspace"
    settings = Settings(
        database_url=database_url,
        workspace=workspace,
        dev_tokens={"gateway-token": "owner-1"},
        sandbox_backend="inprocess",
        test_mode=True,
    )
    gateway = TestClient(create_gateway_app(settings))
    worker = WorkerProcess(database_url, workspace, VerifierProcess(database_url, workspace, monkeypatch))
    return database_url, gateway, worker


def _submit_and_await_approval(gateway: TestClient, worker: WorkerProcess, key: str) -> str:
    created = gateway.post(
        "/flows",
        headers={**_auth(), "Idempotency-Key": key},
        # .env -> HIGH risk, approval required (canon P0: a plain in-workspace write
        # is LOW/auto-approved and would skip the WAITING_APPROVAL leg this helper's
        # callers exercise -- see tests/unit/test_failure_b_approval_resume.py).
        json={"goal": "write a file behind HITL", "path": "result.env", "content": "payload"},
    )
    assert created.status_code == 201
    assert worker.turn().status == TaskState.WAITING_APPROVAL.value
    return str(created.json()["id"])


def test_verifier_rejection_does_not_kill_the_worker_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier owns VERIFYING -> FAILED; the worker must not write it a second time.

    Private criteria are deliberately not seeded, so ``/verify`` rejects with
    "private verifier criteria missing" -- it commits FAILED on its own connection and
    only then answers REPLAN. A worker that re-issues that terminal transition against
    its now-stale task object loses the revision CAS and fails an already-settled job.
    """
    database_url, gateway, worker = _build(tmp_path, monkeypatch)

    with gateway:
        flow_id = _submit_and_await_approval(gateway, worker, "cas-race-reject")
        approval_id = gateway.get(f"/flows/{flow_id}", headers=_auth()).json()["approvals"][0]["id"]
        decided = gateway.post(
            f"/approvals/{approval_id}/decision", headers=_auth(), json={"approve": True}
        )
        assert decided.status_code == 200

        turn = worker.turn()

    assert turn.error is None, f"worker crashed on an already-terminal task: {turn.error}"
    assert turn.status == TaskState.FAILED.value
    assert turn.job_status != "FAILED"
    assert turn.job_error != "revision CAS failed"

    # The rejection is journalled once, by the verifier -- not duplicated by the worker.
    with Database(database_url).session_factory() as session:
        failures = [
            row
            for row in session.query(StateTransition).filter_by(task_id=flow_id).all()
            if row.to_state == TaskState.FAILED.value
        ]
    assert [row.actor for row in failures] == ["verifier-service"]


def test_approval_decision_drives_worker_to_done_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway approves on its connection, the worker finishes on another, verifier sets DONE."""
    database_url, gateway, worker = _build(tmp_path, monkeypatch)

    with gateway:
        flow_id = _submit_and_await_approval(gateway, worker, "cas-race-done")
        seed_private_criteria(database_url, flow_id)
        approval_id = gateway.get(f"/flows/{flow_id}", headers=_auth()).json()["approvals"][0]["id"]
        decided = gateway.post(
            f"/approvals/{approval_id}/decision", headers=_auth(), json={"approve": True}
        )
        assert decided.status_code == 200
        assert decided.json()["decision"] == "APPROVED"

        turn = worker.turn()

    assert turn.error is None, f"worker crashed after approval: {turn.error}"
    assert turn.status == TaskState.DONE.value
    assert turn.job_status == "DONE"

    with Database(database_url).session_factory() as session:
        task = TaskRepository(session).get(flow_id)
        job = session.query(QueueJob).filter_by(task_id=flow_id).one()
        done = [row for row in task.transitions if row.to_state == TaskState.DONE.value]
    assert task.status == TaskState.DONE.value
    assert job.last_error is None
    # AGENTS.md invariant: only the verifier service may write DONE.
    assert [row.actor for row in done] == ["verifier-service"]
    assert (tmp_path / "workspace" / "result.env").read_text(encoding="utf-8") == "payload"
