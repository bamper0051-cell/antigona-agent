"""DEFECT-WAVE0 / CC-02: verifier structural DONE must require byte-equal content,
not just equal SIZE (wrong content of the same length must not reach DONE).

RED phase: the wrong-content task is marked DONE (structural) on current HEAD.
GREEN phase: wrong content falls through to the judge path; exact content
still finalizes structurally without the judge.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from antigona.database import Database
from antigona.models import Artifact, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier import ProviderResult, VerifierCriteriaDatabase, VerifierCriteriaStore
from antigona.verifier_service import create_verifier_app


@dataclass
class _FakeJudge:
    approved: bool
    seen: dict | None = None

    def evaluate(self, **kwargs: object) -> ProviderResult:
        # The verifier calls the judge with keyword args
        # (goal, criteria, actual_content, evidence) — mirror that shape.
        self.seen = kwargs
        return ProviderResult(self.approved, "judge", "verifier")


def _prepare(tmp_path: Path, file_bytes: bytes, task_content: str, goal: str) -> tuple[str, str, Path]:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(CreateTask("owner", goal, "demo.txt", task_content, "idem"))
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING, TaskState.OBSERVING):
            repo.transition(task, state, state.value, "test")
        (workspace / "demo.txt").write_bytes(file_bytes)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="demo.txt",
                sha256=hashlib.sha256(file_bytes).hexdigest(),
                size=len(file_bytes),
                evidence={"sha256": hashlib.sha256(file_bytes).hexdigest()},
            )
        )
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "artifact must match the requested content")
        criteria_session.commit()
    return url, task_id, workspace


def _run_verify(url: str, task_id: str, workspace: Path, judge: _FakeJudge, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(workspace))
    with TestClient(create_verifier_app(url, credential="secret", judge=judge)) as client:
        resp = client.post(
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": "corr"},
        )
    assert resp.status_code == 200
    return resp.json()


def _status(url: str, task_id: str) -> str:
    db = Database(url)
    with db.session_factory() as session:
        task = TaskRepository(session).get(task_id)
        return task.status


# CC-02: LLM-draft goal (no explicit content in the goal), task.content carries
# the draft "ANTIGONA TEST"; the agent wrote "TEST ANTIGONA" — same 13 bytes,
# different content. Current verifier: size==len -> structural DONE (FALSE_DONE).
def test_wrong_content_same_length_never_structural_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal = "создай файл demo.txt"  # LLM-draft: deterministic_expected_content == ""
    judge = _FakeJudge(approved=False)  # judge would REJECT the mismatch
    url, task_id, workspace = _prepare(tmp_path, b"TEST ANTIGONA", "ANTIGONA TEST", goal)
    decision = _run_verify(url, task_id, workspace, judge, monkeypatch)
    status = _status(url, task_id)
    assert decision != {"decision": "DONE"}, "structural DONE on wrong content = FALSE_DONE (CC-02)"
    assert status != TaskState.DONE.value, f"task reached DONE with wrong content: {status}"
    assert judge.seen is not None, "wrong content must reach the judge path"


@pytest.mark.parametrize(
    ("file_bytes", "task_content"),
    [
        # Same six bytes, but distinct content. _norm_content previously made
        # both values "ABCD" by stripping artifact spaces and goal quotes.
        (b" ABCD ", '"ABCD"'),
        # Decode(errors="replace") previously turned this invalid byte into
        # the same U+FFFD string as the requested valid UTF-8 content.
        (b"\xff", "\ufffd"),
    ],
    ids=("same_length_whitespace_quote", "invalid_utf8_replacement_collision"),
)
def test_structural_write_requires_exact_utf8_bytes_not_normalized_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_bytes: bytes,
    task_content: str,
) -> None:
    """Replacing ``data == task.content.encode('utf-8')`` with normalized
    decoded-string comparison must make this test fail: byte-distinct content
    must reach the rejecting judge rather than structural DONE.
    """
    assert file_bytes != task_content.encode("utf-8")
    goal = "создай файл demo.txt"
    judge = _FakeJudge(approved=False)
    url, task_id, workspace = _prepare(tmp_path, file_bytes, task_content, goal)
    decision = _run_verify(url, task_id, workspace, judge, monkeypatch)
    assert decision != {"decision": "DONE"}, "byte-distinct artifact reached structural DONE"
    assert _status(url, task_id) != TaskState.DONE.value
    assert judge.seen is not None, "byte mismatch must reach the judge path"


# Positive: exact content (same length) still finalizes structurally WITHOUT judge.
def test_exact_content_still_structural_done_without_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal = "создай файл demo.txt"
    judge = _FakeJudge(approved=True)
    url, task_id, workspace = _prepare(tmp_path, b"ANTIGONA TEST", "ANTIGONA TEST", goal)
    decision = _run_verify(url, task_id, workspace, judge, monkeypatch)
    assert decision == {"decision": "DONE"}
    assert _status(url, task_id) == TaskState.DONE.value
    assert judge.seen is None, "exact content should not need the judge"
