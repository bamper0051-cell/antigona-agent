"""Unit test for DF-PC-003 (CRITICAL, FALSE_DONE prevention).

Repro / Invariant:
Goal «Создай файл workspace/l4_seq.txt с текстом FIRST, затем замени его содержимое на SECOND»
contains an unplanned subsequent operation (замени -> SECOND).
Single-step execution only creates FIRST; the verifier MUST NOT finalize as DONE.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.models import Artifact, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.task_goal import parse_goal
from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore
from antigona.verifier.judge import ProviderResult
from tests.unit.test_verifier_v2 import FakeProvider, run_verify


def test_df_pc_003_parse_goal_flags_incomplete_sequence() -> None:
    goal = "Создай файл workspace/l4_seq.txt с текстом FIRST, затем замени его содержимое на SECOND"
    plan = parse_goal(goal)

    # Goal has a subsequent operation ('замени') that cannot be completed in a single write step.
    assert plan.incomplete_sequence is True
    assert plan.note == "incomplete_sequence"
    assert plan.path == "l4_seq.txt"
    assert plan.content == "FIRST"


def test_df_pc_003_parse_goal_variants() -> None:
    variants = [
        "Создай l4.txt с текстом FIRST, потом обнови его на SECOND",
        "Создай l4.txt с текстом FIRST, после этого добавь SECOND",
        "Создай l4.txt с текстом FIRST, затем перепиши на SECOND",
    ]
    for goal in variants:
        plan = parse_goal(goal)
        assert plan.incomplete_sequence is True, f"Failed for {goal}"
        assert plan.note == "incomplete_sequence"


def test_df_pc_003_single_write_not_flagged() -> None:
    goal = "Создай файл workspace/single.txt с текстом FIRST"
    plan = parse_goal(goal)
    assert plan.incomplete_sequence is False
    assert plan.note != "incomplete_sequence"
    assert plan.path == "single.txt"
    assert plan.content == "FIRST"


def test_df_pc_003_verifier_rejects_incomplete_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifier must reject (not DONE) when goal contains an unplanned subsequent sequence."""
    url = f"sqlite:///{tmp_path / 'db_df_pc_003.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(url)
    db.create_all()

    goal = "Создай файл workspace/l4_seq.txt с текстом FIRST, затем замени его содержимое на SECOND"
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "l4_seq.txt",
                "FIRST",
                "idem-df-pc-003",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")

        # Step 1 only writes FIRST (the second op 'замени на SECOND' never happened)
        data = b"FIRST"
        (workspace / "l4_seq.txt").write_bytes(data)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="l4_seq.txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                evidence={"sha256": hashlib.sha256(data).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id

    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "file l4_seq.txt must contain SECOND")
        criteria_session.commit()

    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)

    # LOOP ENGINEERING: DONE != proof. Verifier MUST NOT finalize DONE when second op was never done.
    assert payload["decision"] != "DONE", payload
    assert payload["decision"] == "REPLAN"
