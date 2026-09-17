from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from verifier_fakes import seed_private_criteria

from antigona.database import Database
from antigona.models import Artifact, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier import LLMJudge, ProviderResult
from antigona.verifier.judge import JudgeRequest
from antigona.verifier_service import create_verifier_app


@dataclass
class RecordingProvider:
    approved: bool = True
    calls: int = 0

    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult:
        del request
        self.calls += 1
        return ProviderResult(self.approved, "fixed verifier reason", model)


def prepare_verifying_artifact(
    tmp_path: Path,
    *,
    data: bytes = b"safe release evidence",
    relative_path: str = "out.txt",
    criteria: str = "independently seeded criterion",
) -> tuple[str, str, Path]:
    database_url = f"sqlite:///{tmp_path / 'verifier-security.db'}"
    workspace = tmp_path / "workspace"
    artifact_path = workspace / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    database = Database(database_url)
    database.create_all()
    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create(
            CreateTask(
                "owner",
                "publish safe release evidence",
                relative_path,
                "worker-visible input",
                "verifier-security",
            )
        )
        for target in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repository.transition(task, target, target.value, "test")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path=relative_path,
                sha256=digest,
                size=len(data),
                evidence={"sha256": digest},
            )
        )
        repository.transition(task, TaskState.OBSERVING, "observing", "test")
        repository.transition(task, TaskState.VERIFYING, "verifying", "test")
        repository.commit()
        task_id = task.id

    seed_private_criteria(database_url, task_id, criteria)
    return database_url, task_id, workspace


def verifier_app(
    database_url: str,
    workspace: Path,
    provider: RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
    judge = LLMJudge(primary_model="primary", verifier_model="verifier", provider=provider)
    return create_verifier_app(database_url, credential="secret", judge=judge)


def post_verify(client: TestClient, task_id: str):
    return client.post(
        "/verify",
        headers={"Authorization": "Bearer secret"},
        json={"task_id": task_id, "correlation_id": "security-correlation"},
    )


def task_status(database_url: str, task_id: str) -> tuple[str, bool]:
    database = Database(database_url)
    with database.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        artifact = session.scalar(select(Artifact).where(Artifact.task_id == task_id))
        assert artifact is not None
        return task.status, artifact.verified


@pytest.mark.parametrize("link_kind", ["final", "parent"])
@pytest.mark.skipif(sys.platform == "win32", reason='symlink/NOFOLLOW semantics unsupported on Windows (Wave 4)')
def test_verifier_rejects_symlinks_before_any_pathname_read_or_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    relative_path = "out.txt" if link_kind == "final" else "nested/out.txt"
    database_url, task_id, workspace = prepare_verifying_artifact(
        tmp_path,
        relative_path=relative_path,
    )
    artifact = workspace / relative_path
    if link_kind == "final":
        target = workspace / "same-content.txt"
        artifact.replace(target)
        artifact.symlink_to(target.name)
    else:
        real_parent = workspace / "real-parent"
        artifact.parent.replace(real_parent)
        artifact.parent.symlink_to(real_parent.name, target_is_directory=True)

    provider = RecordingProvider()
    app = verifier_app(database_url, workspace, provider, monkeypatch)
    pathname_reads: list[str] = []

    def forbidden_pathname_read(path: Path, *_args: object, **_kwargs: object):
        pathname_reads.append(str(path))
        raise AssertionError("pathname inspection must not precede descriptor read")

    with TestClient(app) as client:
        with monkeypatch.context() as path_guard:
            path_guard.setattr(Path, "resolve", forbidden_pathname_read)
            path_guard.setattr(Path, "is_file", forbidden_pathname_read)
            path_guard.setattr(Path, "read_bytes", forbidden_pathname_read)
            response = post_verify(client, task_id)

    assert response.status_code == 409
    assert response.json() == {"detail": "artifact verification failed"}
    assert pathname_reads == []
    assert provider.calls == 0
    assert task_status(database_url, task_id) == (TaskState.FAILED.value, False)


def test_verifier_rejects_deterministic_final_name_swap_without_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, task_id, workspace = prepare_verifying_artifact(tmp_path)
    artifact = workspace / "out.txt"
    replacement = workspace / "replacement.txt"
    replacement.write_bytes(b"safe release evidence")
    real_read = os.read
    swapped = False

    def swapping_read(fd: int, size: int) -> bytes:
        nonlocal swapped
        data = real_read(fd, size)
        if not swapped:
            swapped = True
            replacement.replace(artifact)
        return data

    monkeypatch.setattr("antigona.verifier_service.os.read", swapping_read)
    provider = RecordingProvider()
    app = verifier_app(database_url, workspace, provider, monkeypatch)

    with TestClient(app) as client:
        response = post_verify(client, task_id)

    assert swapped is True
    assert response.status_code == 409
    assert response.json() == {"detail": "artifact verification failed"}
    assert provider.calls == 0
    assert task_status(database_url, task_id) == (TaskState.FAILED.value, False)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b" \n\t ",
        b'Traceback (most recent call last):\n  File "worker.py", line 1\nboom',
        b"API_KEY=synthetic-fully-redacted-marker",
    ],
)
def test_structurally_unusable_artifact_is_rejected_before_approving_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
) -> None:
    database_url, task_id, workspace = prepare_verifying_artifact(
        tmp_path,
        data=data,
        criteria="required nonempty evidence seeded before verification",
    )
    provider = RecordingProvider(approved=True)
    app = verifier_app(database_url, workspace, provider, monkeypatch)

    with TestClient(app) as client:
        response = post_verify(client, task_id)

    assert response.status_code == 200
    assert response.json() == {"decision": "REPLAN"}
    assert provider.calls == 0
    assert task_status(database_url, task_id) == (TaskState.FAILED.value, False)


def test_nonempty_safe_artifact_keeps_normal_verifier_path_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"safe release evidence"
    database_url, task_id, workspace = prepare_verifying_artifact(
        tmp_path,
        data=data,
        criteria=data.decode(),
    )
    provider = RecordingProvider(approved=True)
    app = verifier_app(database_url, workspace, provider, monkeypatch)

    with TestClient(app) as client:
        response = post_verify(client, task_id)

    assert response.status_code == 200
    assert response.json() == {"decision": "DONE"}
    assert provider.calls == 1
    assert task_status(database_url, task_id) == (TaskState.DONE.value, True)
