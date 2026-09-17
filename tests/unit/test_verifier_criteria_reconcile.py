"""P0 fix (§2 fail-closed contract): a task whose verifier criteria could not be
recorded must never be queued for execution.

Corrected root cause (see audit follow-up): the originally-suspected HITL-bypass
via ``evaluate_risk``/``ConfirmationPolicy`` did NOT reproduce — that mechanism is
deliberately designed and covered by tests/unit/test_hitl.py (canon P0: safe
in-workspace write = LOW = auto-approve). The real, reproducible bug is here:
``TaskSubmissionService.submit()`` (src/antigona/core/task_service.py) opens a
SEPARATE ``VerifierCriteriaDatabase`` engine against ``ANTIGONA_DATABASE_URL`` and
silently swallows any failure writing the task's verifier criteria row
(``except Exception: logger.warning(...)``), then unconditionally enqueues the
task for execution anyway. In a deployment where the main app process (gateway /
worker) has run ``Database.create_all()`` but the separate ``antigona-verifier``
process has never started yet, the ``verifier_criteria`` table (owned by its own
disjoint ``CriteriaBase`` metadata, never created by ``Database.create_all()``)
does not exist — the write raises, is swallowed, and the task is queued and
executed with no criteria recorded for it to ever be judged against.

The verifier service itself already fails closed on this (``VerifierCriteriaStore
.require()`` raises ``MissingVerifierCriteria`` -> ``reject()``, never DONE) — so
this was never a silent-DONE security bypass. But the task's side effects (e.g.
the file write) still execute before that eventual, confusing downstream reject.
Fail-closed here means: don't execute a task we already know cannot be verified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.models import TaskState
from antigona.queue import QueueJob
from antigona.repository import TaskRepository
from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore


def _shared_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{tmp_path / name}"


def test_schema_missing_self_heals_and_still_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-init unification: verifier_criteria absent at submit time is no
    longer a bug -- the private engine creates it itself (idempotent) and the
    task queues normally, closing the first-boot ordering race."""
    db_url = _shared_url(tmp_path, "shared_missing.sqlite")
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", db_url)
    db = Database(db_url)
    db.create_all()  # main schema only; verifier_criteria intentionally NOT pre-created

    service = TaskSubmissionService(db)
    result = service.submit(owner_id="owner", message="write x.txt", path="x.txt", content="hello")

    assert result["status"] == TaskState.QUEUED.value
    criteria_db = VerifierCriteriaDatabase(db_url)
    with criteria_db.session_factory() as session:
        assert VerifierCriteriaStore(session).require(result["id"]) == (
            "Goal satisfied: write x.txt"
        )


def test_task_not_queued_when_verifier_criteria_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed defense in depth: even with the schema present, if the
    criteria write itself fails (disk full, permission, corruption, ...), the
    task must not be enqueued for execution."""
    from antigona.verifier import VerifierCriteriaStore

    db_url = _shared_url(tmp_path, "shared_write_fails.sqlite")
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", db_url)
    db = Database(db_url)
    db.create_all()

    def _boom(self: VerifierCriteriaStore, task_id: str, criteria: str) -> None:
        raise RuntimeError("simulated criteria write failure (disk full)")

    monkeypatch.setattr(VerifierCriteriaStore, "put", _boom)

    service = TaskSubmissionService(db)
    result = service.submit(owner_id="owner", message="write x.txt", path="x.txt", content="hello")

    assert result["status"] != TaskState.QUEUED.value, (
        f"task must not reach QUEUED when verifier criteria write fails, got {result['status']}"
    )
    assert result["status"] != TaskState.DONE.value

    with db.session_factory() as session:
        jobs = session.query(QueueJob).filter_by(task_id=result["id"]).all()
        assert jobs == [], "task must not be enqueued for execution when verifier criteria write fails"


def test_task_cancelled_reason_recorded_when_criteria_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed path must land in a terminal state with an audit reason,
    not silently stuck ambiguous."""
    from antigona.verifier import VerifierCriteriaStore

    db_url = _shared_url(tmp_path, "shared_missing2.sqlite")
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", db_url)
    db = Database(db_url)
    db.create_all()

    def _boom(self: VerifierCriteriaStore, task_id: str, criteria: str) -> None:
        raise RuntimeError("simulated criteria write failure (disk full)")

    monkeypatch.setattr(VerifierCriteriaStore, "put", _boom)

    service = TaskSubmissionService(db)
    result = service.submit(owner_id="owner", message="write z.txt", path="z.txt", content="hello")

    with db.session_factory() as session:
        task = TaskRepository(session).get(result["id"])
        assert task.status == TaskState.CANCELLED.value
        assert task.steps
        assert {step.status for step in task.steps} == {"CANCELLED"}
        assert task.transitions, "expected a recorded transition for the fail-closed cancel"
        reason = task.transitions[-1].reason
        assert reason and "criteria" in reason.lower()
        assert "disk full" not in reason.lower()


def test_task_still_queued_when_verifier_criteria_write_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression control: happy path (criteria schema present) unaffected."""
    db_url = _shared_url(tmp_path, "shared_ok.sqlite")
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", db_url)
    db = Database(db_url)
    db.create_all()
    VerifierCriteriaDatabase(db_url).create_all()

    service = TaskSubmissionService(db)
    result = service.submit(owner_id="owner", message="write y.txt", path="y.txt", content="hello")

    assert result["status"] == TaskState.QUEUED.value


def test_same_idempotency_key_replays_cancelled_task_without_retrying_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal infrastructure failure is replayed, not duplicated or re-enqueued."""
    db_url = _shared_url(tmp_path, "shared_replay_cancelled.sqlite")
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", db_url)
    db = Database(db_url)
    db.create_all()
    calls = 0

    def _boom(self: VerifierCriteriaStore, task_id: str, criteria: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated criteria write failure (disk full)")

    monkeypatch.setattr(VerifierCriteriaStore, "put", _boom)
    service = TaskSubmissionService(db)
    first = service.submit(
        owner_id="owner",
        message="write replay.txt",
        path="replay.txt",
        content="hello",
        idempotency_key="criteria-replay-1",
    )

    with db.session_factory() as session:
        first_task = TaskRepository(session).get(first["id"])
        first_transition_count = len(first_task.transitions)

    replay = service.submit(
        owner_id="owner",
        message="write replay.txt",
        path="replay.txt",
        content="hello",
        idempotency_key="criteria-replay-1",
    )

    assert replay["id"] == first["id"]
    assert replay["created"] is False
    assert replay["status"] == TaskState.CANCELLED.value
    assert calls == 1
    with db.session_factory() as session:
        task = TaskRepository(session).get(first["id"])
        assert len(task.transitions) == first_transition_count
        assert session.query(QueueJob).filter_by(task_id=task.id).all() == []
