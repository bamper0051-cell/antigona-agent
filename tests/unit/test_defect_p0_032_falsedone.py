"""Focused RED reproduction and regression tests for DEFECT-P0-032.

Defect: FALSE_DONE / WRONG TARGET PATH / VERIFIER POSTCONDITION FAILURE
- BUG-A: parse_goal extracts wrong path (e.g. JSON key "name") from goal text.
- BUG-B: Verifier marks task DONE even when requested target path/content was not created.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from antigona.database import Database
from antigona.models import Artifact, FlowStep, TaskFlow, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.task_goal import expected_paths_from_goal, parse_goal
from antigona.verifier import (
    LLMJudge,
    ProviderResult,
    VerifierCriteriaDatabase,
    VerifierCriteriaStore,
    VerifierProvider,
)
from antigona.verifier_service import create_verifier_app


class FakePassingJudgeProvider(VerifierProvider):
    def evaluate(self, request: object, *, model: str) -> ProviderResult:
        return ProviderResult(True, "passed", "verifier")


class FakeFailingJudgeProvider(VerifierProvider):
    def evaluate(self, request: object, *, model: str) -> ProviderResult:
        return ProviderResult(False, "content mismatch", "verifier")


@pytest.fixture
def test_env(tmp_path: Path) -> Generator[dict[str, object], None, None]:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(url)
    db.create_all()

    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()

    yield {
        "url": url,
        "workspace": workspace,
        "db": db,
        "criteria_db": criteria_db,
        "tmp_path": tmp_path,
    }


def _setup_verifying_task(
    env: dict[str, object],
    *,
    goal: str,
    target_path: str,
    content: str,
    written_file_rel: str | None,
    written_data: bytes | None,
    artifact_path: str | None = None,
) -> tuple[str, str]:
    db = env["db"]
    workspace = env["workspace"]
    assert isinstance(workspace, Path)
    assert isinstance(db, Database)

    if written_file_rel is not None and written_data is not None:
        target_file = workspace / written_file_rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(written_data)

    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal=goal,
                path=target_path,
                content=content,
                idempotency_key=f"idem-{os.urandom(8).hex()}",
                tool_name="workspace.write_text",
            )
        )
        for s in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, s, s.value, "test")

        step = FlowStep(task_id=task.id, index=0, tool_name="workspace.write_text")
        session.add(step)
        session.flush()

        effective_art_path = artifact_path or target_path
        art_bytes = written_data or b""
        art = Artifact(
            task_id=task.id,
            step_id=step.id,
            path=effective_art_path,
            sha256=hashlib.sha256(art_bytes).hexdigest(),
            size=len(art_bytes),
            evidence={"sha256": hashlib.sha256(art_bytes).hexdigest()},
        )
        session.add(art)

        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        session.commit()
        task_id = task.id

    criteria_db = env["criteria_db"]
    assert isinstance(criteria_db, VerifierCriteriaDatabase)
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "verify postcondition")
        criteria_session.commit()

    return task_id, str(env["url"])


def _call_verify(
    env: dict[str, object],
    task_id: str,
    provider: VerifierProvider | None = None,
) -> tuple[int, dict[str, str], str]:
    workspace = env["workspace"]
    url = str(env["url"])
    assert isinstance(workspace, Path)

    _prev_ws = os.environ.get("ANTIGONA_WORKSPACE")
    os.environ["ANTIGONA_WORKSPACE"] = str(workspace)
    try:
        judge = LLMJudge(
            primary_model="primary",
            verifier_model="verifier",
            provider=provider or FakePassingJudgeProvider(),
        )
        app = create_verifier_app(url, credential="secret", judge=judge)
        with TestClient(app) as client:
            resp = client.post(
                "/verify",
                headers={"Authorization": "Bearer secret"},
                json={"task_id": task_id, "correlation_id": "corr-test"},
            )
            status_code = resp.status_code
            payload = resp.json() if status_code == 200 else {}

        db = env["db"]
        assert isinstance(db, Database)
        with db.session_factory() as session:
            t = session.get(TaskFlow, task_id)
            assert t is not None
            final_status = t.status

        return status_code, payload, final_status
    finally:
        # Restore the previous workspace so this test does not leak
        # ANTIGONA_WORKSPACE into later tests (fixes test_t9_runtime_roots_identical
        # env pollution when this module runs earlier in the suite).
        if _prev_ws is None:
            os.environ.pop("ANTIGONA_WORKSPACE", None)
        else:
            os.environ["ANTIGONA_WORKSPACE"] = _prev_ws


# ==============================================================================
# LEVEL 1: RED-1 — Path & Content Argument Extraction Contract (BUG-A)
# ==============================================================================


@pytest.mark.parametrize(
    ("goal", "expected_path", "expected_content"),
    [
        (
            'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}',
            "exam/p0/data.json",
            '{"name":"antigona","value":42}',
        ),
        (
            'Создай JSON в exam/p0/data.json: {"name": "antigona"}',
            "exam/p0/data.json",
            '{"name": "antigona"}',
        ),
        (
            'Запиши {"name": "antigona", "val": 1} в файл exam/p0/data.json',
            "exam/p0/data.json",
            '{"name": "antigona", "val": 1}',
        ),
        (
            'Создай JSON объект {"name": "antigona"} в exam/p0/data.json',
            "exam/p0/data.json",
            '{"name": "antigona"}',
        ),
        (
            'Создай JSON {"name": "antigona"} в файле exam/p0/data.json',
            "exam/p0/data.json",
            '{"name": "antigona"}',
        ),
        (
            'Запиши в exam/p0/data.json: {"name": "antigona"}',
            "exam/p0/data.json",
            '{"name": "antigona"}',
        ),
        (
            'Создай конфигурационный JSON файл exam/p0/data.json: {"name": "antigona"}',
            "exam/p0/data.json",
            '{"name": "antigona"}',
        ),
        (
            'Создай JSON файл exam/p0/a.txt: {"test": 1}',
            "exam/p0/a.txt",
            '{"test": 1}',
        ),
        (
            'Создай JSON файл nested/dir/file.json: {"key": "value"}',
            "nested/dir/file.json",
            '{"key": "value"}',
        ),
        (
            'Создай JSON файл simple.txt: {"status": "ok"}',
            "simple.txt",
            '{"status": "ok"}',
        ),
    ],
)
def test_red_1_path_and_content_extraction(
    goal: str, expected_path: str, expected_content: str
) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "file_write", f"Intent mismatch for goal: {goal}"
    assert plan.path == expected_path, (
        f"RED-1: Expected path {expected_path!r}, but got {plan.path!r} for goal: {goal}"
    )
    assert expected_path in expected_paths_from_goal(goal), (
        f"RED-1: {expected_path!r} must be in expected_paths_from_goal"
    )
    if expected_content:
        try:
            assert json.loads(plan.content) == json.loads(expected_content)
        except Exception:
            assert plan.content == expected_content


# ==============================================================================
# LEVEL 2: RED-2 — Verifier Exact-Target Postcondition Contract (BUG-B)
# ==============================================================================


@pytest.mark.parametrize(
    ("goal", "wrong_written_path"),
    [
        ('Создай JSON в exam/p0/data.json: {"name": "antigona"}', "name"),
        ('Запиши {"name": "antigona", "val": 1} в файл exam/p0/data.json', "name"),
        ('Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}', "name"),
        ('Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}', "wrong/data.json"),
    ],
)
def test_red_2_verifier_rejects_wrong_target_path(
    test_env: dict[str, object], goal: str, wrong_written_path: str
) -> None:
    """If tool writes to 'name' or 'wrong/data.json' instead of 'exam/p0/data.json', verifier must REJECT."""
    content = '{"name":"antigona","value":42}'

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=wrong_written_path,
        content=content,
        written_file_rel=wrong_written_path,
        written_data=content.encode("utf-8"),
        artifact_path=wrong_written_path,
    )

    status_code, payload, final_status = _call_verify(test_env, task_id)
    assert payload == {"decision": "REPLAN"}
    assert final_status != TaskState.DONE.value, (
        f"RED-2: Verifier must NOT mark task DONE when artifact is written to wrong path {wrong_written_path!r}!"
    )
    assert final_status == TaskState.FAILED.value


def test_red_2_verifier_rejects_when_requested_target_missing(
    test_env: dict[str, object],
) -> None:
    """If tool claims success but requested file does not exist on disk, verifier must REJECT."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    target_path = "exam/p0/data.json"
    content = '{"name":"antigona","value":42}'

    # Do not write file to disk
    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content=content,
        written_file_rel=None,
        written_data=content.encode("utf-8"),
    )

    status_code, payload, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value, (
        "RED-2: Verifier must NOT mark task DONE when target file is missing on disk!"
    )
    assert final_status == TaskState.FAILED.value


# ==============================================================================
# LEVEL 3: RED-3 — Finalizer FALSE_DONE Invariant
# ==============================================================================


@pytest.mark.parametrize(
    ("goal", "written_rel", "expected_rel"),
    [
        (
            "Создай файл exam/p0/data.json с текстом: hello",
            "wrong/path.txt",
            "exam/p0/data.json",
        ),
        (
            "Создай файл simple.txt с текстом: ok",
            "other.txt",
            "simple.txt",
        ),
        (
            "Создай файл nested/dir/file.json: {}",
            "nested/other/file.json",
            "nested/dir/file.json",
        ),
    ],
)
def test_red_3_terminal_state_never_done_on_path_mismatch(
    test_env: dict[str, object], goal: str, written_rel: str, expected_rel: str
) -> None:
    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=written_rel,
        content="data",
        written_file_rel=written_rel,
        written_data=b"data",
        artifact_path=written_rel,
    )
    _, payload, final_status = _call_verify(test_env, task_id)
    assert final_status in (TaskState.FAILED.value, TaskState.CANCELLED.value), (
        f"RED-3: Terminal state must be FAILED, not {final_status}"
    )
    assert final_status != TaskState.DONE.value


# ==============================================================================
# FALSE_DONE ADVERSARIAL MATRIX (A through H)
# ==============================================================================


def test_matrix_a_correct_target_correct_content_allows_done(
    test_env: dict[str, object],
) -> None:
    """Matrix A: correct target + correct content -> DONE allowed."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    target_path = "exam/p0/data.json"
    content = '{"name":"antigona","value":42}'

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content=content,
        written_file_rel=target_path,
        written_data=content.encode("utf-8"),
        artifact_path=target_path,
    )
    status_code, payload, final_status = _call_verify(test_env, task_id)
    assert payload == {"decision": "DONE"}
    assert final_status == TaskState.DONE.value


def test_matrix_b_wrong_target_tool_success_not_done(
    test_env: dict[str, object],
) -> None:
    """Matrix B: wrong target + tool success -> NOT DONE."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    wrong_path = "name"
    content = '{"name":"antigona","value":42}'

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=wrong_path,
        content=content,
        written_file_rel=wrong_path,
        written_data=content.encode("utf-8"),
        artifact_path=wrong_path,
    )
    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value
    assert final_status == TaskState.FAILED.value


def test_matrix_c_missing_target_tool_success_not_done(
    test_env: dict[str, object],
) -> None:
    """Matrix C: missing target + tool success -> NOT DONE."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    target_path = "exam/p0/data.json"
    content = '{"name":"antigona","value":42}'

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content=content,
        written_file_rel=None,  # Not written to disk
        written_data=content.encode("utf-8"),
        artifact_path=target_path,
    )
    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value


def test_matrix_d_correct_target_wrong_content_not_done(
    test_env: dict[str, object],
) -> None:
    """Matrix D: correct target + wrong content -> NOT DONE when exact content required."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    target_path = "exam/p0/data.json"
    actual_content = '{"name":"antigona","value":999}'  # Mismatched content

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content=actual_content,
        written_file_rel=target_path,
        written_data=actual_content.encode("utf-8"),
        artifact_path=target_path,
    )
    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value
    assert final_status == TaskState.FAILED.value


def test_matrix_e_tool_error_not_done(test_env: dict[str, object]) -> None:
    """Matrix E: tool error (artifact hash mismatch / unreadable) -> NOT DONE."""
    goal = "Создай файл exam/p0/data.json с текстом: hello"
    target_path = "exam/p0/data.json"

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content="hello",
        written_file_rel=target_path,
        written_data=b"corrupted_bytes",
        artifact_path=target_path,
    )
    db = test_env["db"]
    assert isinstance(db, Database)
    with db.session_factory() as session:
        art = session.scalar(select(Artifact).where(Artifact.task_id == task_id))
        assert art is not None
        art.sha256 = "invalid_hash_value"
        session.commit()

    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value
    assert final_status == TaskState.FAILED.value


def test_matrix_f_verifier_error_not_done(test_env: dict[str, object]) -> None:
    """Matrix F: verifier error / judge rejection -> NOT DONE."""
    goal = "Создай файл exam/p0/notes.txt с описанием архитектуры"
    target_path = "exam/p0/notes.txt"

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content="brief note",
        written_file_rel=target_path,
        written_data=b"brief note",
        artifact_path=target_path,
    )
    _, _, final_status = _call_verify(
        test_env, task_id, provider=FakeFailingJudgeProvider()
    )
    assert final_status != TaskState.DONE.value
    assert final_status == TaskState.FAILED.value


def test_matrix_g_artifact_record_exists_but_target_absent(
    test_env: dict[str, object],
) -> None:
    """Matrix G: artifact record exists in DB but target file is deleted/absent -> NOT DONE."""
    goal = "Создай файл exam/p0/a.txt с текстом: test"
    target_path = "exam/p0/a.txt"

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content="test",
        written_file_rel=None,  # Absent from disk
        written_data=b"test",
        artifact_path=target_path,
    )
    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value


def test_matrix_h_symlink_or_hardlink_target_rejected(
    test_env: dict[str, object],
) -> None:
    """Matrix H: symlink or hardlink target is rejected by verifier."""
    goal = "Создай файл exam/p0/safe.txt с текстом: secure"
    target_path = "exam/p0/safe.txt"
    workspace = test_env["workspace"]
    assert isinstance(workspace, Path)

    outside = test_env["tmp_path"] / "outside_stale.txt"  # type: ignore[operator]
    outside.write_bytes(b"secure")

    link_target = workspace / "exam" / "p0" / "safe.txt"
    link_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_target.hardlink_to(outside)
    except OSError:
        pytest.skip("Hardlinks not supported on this filesystem")

    task_id, _ = _setup_verifying_task(
        test_env,
        goal=goal,
        target_path=target_path,
        content="secure",
        written_file_rel=None,
        written_data=b"secure",
        artifact_path=target_path,
    )
    _, _, final_status = _call_verify(test_env, task_id)
    assert final_status != TaskState.DONE.value
