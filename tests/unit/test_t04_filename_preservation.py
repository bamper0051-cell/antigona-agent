"""T04 filename preservation regressions for Telegram/Gateway task path."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from antigona.core.brain import AntigonaBrain
from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.repository import SensitiveTaskInput
from antigona.sessions.repository import SessionRepository

T04_GOAL = "Создай в workspace файл с именем manual test.txt. Не изменяй имя файла."


@pytest.mark.asyncio
async def test_t04_brain_submits_exact_space_preserving_filename(tmp_path) -> None:
    repo = SessionRepository(db_path=str(tmp_path / "s.db"))
    engine = MagicMock()
    engine.reply = AsyncMock(return_value="")
    brain = AntigonaBrain(
        dialogue_engine=engine,
        session_repository=repo,
        db_path=str(tmp_path / "s.db"),
    )
    backend = MagicMock()
    backend.submit_task = AsyncMock(return_value={"flow_id": "flow-t04", "requires_approval": False})
    brain.task_backend = backend
    try:
        await brain.connect()
        await brain.process(
            text=T04_GOAL,
            user_id="owner",
            channel="telegram",
            session_id="telegram:owner",
        )
    finally:
        await brain.close()

    assert backend.submit_task.await_count == 1
    kwargs = backend.submit_task.call_args.kwargs
    assert kwargs["path"] == "manual test.txt"
    assert kwargs["message"] == T04_GOAL


def test_t04_service_creates_flow_and_step_with_exact_filename(tmp_path) -> None:
    from antigona.repository import TaskRepository

    db = Database(f"sqlite:///{tmp_path / 'antigona.db'}")
    db.create_all()
    service = TaskSubmissionService(db)

    result = service.submit(
        owner_id="owner",
        message=T04_GOAL,
        idempotency_key="t04-exact",
        tool_name="workspace.write_text",
        path="manual test.txt",
        content="",
        client="unit",
    )

    with db.session_factory() as session:
        task = TaskRepository(session).get(result["flow_id"])
        assert task.target_path == "manual test.txt"
        assert task.artifacts == []
        assert task.steps[0].arguments["path"] == "manual test.txt"


def test_t04_service_rejects_success_when_goal_path_disagrees_with_write_target(tmp_path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'antigona.db'}")
    db.create_all()
    service = TaskSubmissionService(db)

    with pytest.raises(SensitiveTaskInput, match="goal path does not match target path"):
        service.submit(
            owner_id="owner",
            message=T04_GOAL,
            idempotency_key="t04-mismatch",
            tool_name="workspace.write_text",
            path="test.txt",
            content="Файл с именем manual test.txt создан.",
            client="unit",
        )
