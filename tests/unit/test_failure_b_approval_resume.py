"""FAILURE B (MASTER LOOP ENGINEERING v2.1, guio.md) — approval resume SAME task.

guio.md: «approval is granted, but the original task stays QUEUED or is marked
"not executed". Never create a replacement task after approval. Resume the
original task only.»

This test drives the TWO-PHASE live path (unlike the single-session happy path in
test_orchestrator.py):
  phase 1 — task submitted → orchestrator _resume → PLANNING → request_approval
            → WAITING_APPROVAL (job stays WAITING)
  phase 2 — a SEPARATE decision (owner approves via gateway) → decide_approval
            → enqueue SAME task → worker claims → _resume → dispatch

Invariant under test: the SAME task_id + correlation_id survives the whole chain;
no replacement task is ever created; the original task resumes and reaches
execution (not stuck QUEUED / WAITING_APPROVAL).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from antigona.database import Database
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import TaskFlow, TaskState
from antigona.orchestrator import Orchestrator
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier_client import VerifierClient
from antigona.worker.hitl import ConfirmationPolicyMode, set_confirmation_policy


class _StubVerifier(VerifierClient):
    def __init__(self) -> None:  # noqa: D107 — stub, bypass url/credential
        pass

    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return "DONE"


@pytest.fixture(autouse=True)
def _force_always_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM/HIGH risk tools must require approval so the WAITING_APPROVAL path
    runs, and the canonical default ``ApprovalGrantStore`` (minted by
    ``TaskRepository`` with no explicit ``db_path``) must resolve inside this
    test's ``tmp_path`` — never the live project grant database.
    """
    monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
    monkeypatch.setenv("ANTIGONA_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(tmp_path / "workspace"))
    set_confirmation_policy(ConfirmationPolicyMode.ALWAYS)


def _mkdb(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{tmp_path/'failure_b.db'}")
    db.create_all()
    return db


def _enqueue(session: Session, task: TaskFlow) -> None:
    from antigona.queue import DurableQueue

    DurableQueue(session).enqueue(task)


def _orch(session: Session, tmp_path: Path) -> Orchestrator:
    tool = WorkspaceFileTool(InProcessTestBackend(tmp_path / "ws", test_mode=True), 30)
    return Orchestrator(
        session,
        tool,
        _StubVerifier(),
        lease_seconds=5,
        max_retries=0,
        workspace=None,
    )


def test_approval_resume_keeps_same_task_id_and_reaches_execution(
    tmp_path: Path,
) -> None:
    """Двухфазный approval: после одобрения исходная задача резюмируется (SAME id),
    не создаётся replacement, достигает TOOL_EXECUTING (не застревает QUEUED)."""

    db = _mkdb(tmp_path)
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            # *.env write → HIGH risk, approval required. A plain in-workspace
            # write is LOW/auto-approved (canon P0) and would skip the
            # WAITING_APPROVAL leg this test exercises.
            CreateTask("owner", "write proof.env with data", "proof.env", "data", "key")
        )
        original_id = task.id
        _enqueue(session, task)

    # Phase 1: worker claims, _resume drives to WAITING_APPROVAL (job WAITING).
    with db.session_factory() as session:
        repo = TaskRepository(session)
        res = _orch(session, tmp_path).run(repo.get(original_id), "worker-1")
        assert res.status == TaskState.WAITING_APPROVAL.value, (
            f"expected WAITING_APPROVAL after phase 1, got {res.status}"
        )

    # Phase 2: a SEPARATE decision — owner approves via gateway, then re-enqueue.
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task = repo.get(original_id)
        ap = task.approvals[0]
        assert ap.decision == "PENDING"
        repo.decide_approval(task, ap.id, "owner", True)
        _enqueue(session, task)

    # The decision minted a durable grant through the canonical default
    # ApprovalGrantStore(); with ANTIGONA_PROJECT_ROOT redirected it must have
    # landed inside this test's tmp_path, proving the real default-store path
    # was exercised without touching the live project database.
    assert (tmp_path / "antigona.db").is_file()

    # Worker claims again and _resume must resume the SAME task (dispatch → forward).
    with db.session_factory() as session:
        repo = TaskRepository(session)
        res = _orch(session, tmp_path).run(repo.get(original_id), "worker-1")
        # The original task must have resumed past approval — NOT stuck QUEUED /
        # WAITING_APPROVAL, and NOT replaced by a new task.
        assert res.id == original_id, "replacement task created — FAILURE B"
        assert res.status not in (
            TaskState.QUEUED.value,
            TaskState.WAITING_APPROVAL.value,
            TaskState.PLANNING.value,
        ), f"original task stuck after approval, got {res.status}"
        # It must have moved FORWARD toward execution/verification.
        assert res.status in (
            TaskState.TOOL_EXECUTING.value,
            TaskState.OBSERVING.value,
            TaskState.VERIFYING.value,
            TaskState.DONE.value,
        ), f"original task did not progress past approval, got {res.status}"


def test_approval_resume_never_creates_second_task(tmp_path: Path) -> None:
    """После одобрения в БД остаётся РОВНО одна задача (тот же id), не дубль."""
    db = _mkdb(tmp_path)
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask("owner", "write out.txt", "out.txt", "x", "k")
        )
        original_id = task.id
        _enqueue(session, task)

    with db.session_factory() as session:
        repo = TaskRepository(session)
        task = repo.get(original_id)
        ap = repo.request_approval(task)
        if ap.decision == "PENDING":
            repo.decide_approval(task, ap.id, "owner", True)

    with db.session_factory() as session:
        from sqlalchemy import func, select

        from antigona.models import TaskFlow

        count = session.scalar(select(func.count()).select_from(TaskFlow))
        assert count == 1, f"expected exactly 1 task, got {count} (replacement created)"
