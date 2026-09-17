"""LOOP3 / DEFECT 3 — целостность содержимого файла.

Регрессия живой задачи 4857f8d1 (2026-08-16 04:43): владелец попросил записать
в ``manual_test.txt`` две строки, а на диск попал ВЕСЬ текст инструкции, потому
что ``draft_file_content`` вернул ``None`` (провайдер ответил HTTP 200 с пустым
``message.content``) и сработал безусловный fallback ``content=message``.

Контракт, который закрепляют эти тесты:

1. черновик — вопрос-уточнение → задача НЕ создаётся, владелец получает
   clarification;
2. черновик валиден → ``task.content`` == черновик;
3. черновик ``None`` при ОТВЕТИВШЕМ провайдере (пустой вывод) → задача НЕ
   создаётся, clarification;
4. черновик ``None`` без провайдера (degraded/offline) → задача создаётся с
   ``content == message`` (прежнее поведение сохранено);
5. путь записи не изменён: ``workspace.write_text`` пишет ровно ``task.content``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from antigona.conversation.dialogue_engine import (
    DRAFT_OK,
    DRAFT_REJECTED,
    DRAFT_UNAVAILABLE,
    DialogueEngine,
    FileContentDraft,
    looks_like_clarification,
)
from antigona.core.brain import AntigonaBrain, ResponseType

_REQUEST = "Создай файл manual_test.txt и запиши туда две строки"


class _RecordingBackend:
    """TaskBackend, запоминающий, с каким content создавалась задача."""

    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        tool_name: str | None = None,
        command: tuple[str, ...] = (),
        path: str | None = None,
        content: str | None = None,
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.submits.append({"message": message, "content": content, "path": path})
        return {
            "flow_id": "flow-1",
            "id": "flow-1",
            "status": "QUEUED",
            "requires_approval": False,
        }

    async def cancel_flow(self, flow_id: str) -> Any:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED"})()


class _StubEngine:
    """DialogueEngine-заглушка с управляемым результатом черновика."""

    def __init__(self, draft: FileContentDraft) -> None:
        self._draft = draft
        self.calls = 0

    async def draft_file_content_result(
        self, text: str, session_id: str = "cli-session"
    ) -> FileContentDraft:
        self.calls += 1
        return self._draft

    async def draft_file_content(
        self, text: str, session_id: str = "cli-session"
    ) -> str | None:
        return (await self.draft_file_content_result(text, session_id)).content

    async def reply(self, text: str, session_id: str = "cli-session", context: Any = None) -> str:
        return "ok"

    async def close(self) -> None:
        return None


async def _run_turn(draft: FileContentDraft) -> tuple[Any, _RecordingBackend, _StubEngine]:
    backend = _RecordingBackend()
    engine = _StubEngine(draft)
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name, task_backend=backend, dialogue_engine=engine
        )
        await brain.connect()
        try:
            response = await brain.process(
                text=_REQUEST,
                user_id="owner",
                channel="cli",
                session_id="owner:session",
                context={"owner_id": "owner", "correlation_id": "corr-0001"},
            )
        finally:
            await brain.close()
    return response, backend, engine


# ── 1. Вопрос модели не становится содержимым файла ──────────────────────────


@pytest.mark.asyncio
async def test_clarifying_question_does_not_create_task() -> None:
    response, backend, engine = await _run_turn(
        FileContentDraft(None, DRAFT_REJECTED)
    )

    assert engine.calls == 1
    assert backend.submits == [], "задача не должна создаваться"
    assert response.response_type == ResponseType.CLARIFICATION
    assert "содержимое файла" in response.text.lower()


def test_question_draft_is_rejected_by_heuristic() -> None:
    assert looks_like_clarification("Какой именно текст записать в файл?")
    assert looks_like_clarification("Уточните, пожалуйста, содержимое.")
    assert looks_like_clarification("? что писать")
    assert looks_like_clarification("")
    # Нормальное содержимое проходит.
    assert not looks_like_clarification("ANTIGONA FILE TEST\n12345")
    # Длинный документ со словом «укажите» внутри — это содержимое, не вопрос.
    long_doc = "\n".join(["Инструкция по заполнению формы."] + [f"Пункт {i}: укажите значение." for i in range(8)])
    assert not looks_like_clarification(long_doc)


@pytest.mark.asyncio
async def test_engine_rejects_question_from_provider() -> None:
    """Полный путь движка: провайдер вернул вопрос → DRAFT_REJECTED."""

    class _QuestionProvider:
        def generate(self, messages: list[dict[str, str]], context: Any = None) -> str:
            return "Какой именно текст записать в файл?"

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_QuestionProvider())
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert draft.content is None
    assert draft.status == DRAFT_REJECTED


@pytest.mark.asyncio
async def test_engine_rejects_empty_provider_output() -> None:
    """Наблюдаемый живой отказ: HTTP 200 с пустым message.content."""

    class _EmptyProvider:
        def generate(self, messages: list[dict[str, str]], context: Any = None) -> str:
            return ""

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_EmptyProvider())
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert draft.content is None
    assert draft.status == DRAFT_REJECTED


@pytest.mark.asyncio
async def test_engine_reports_unavailable_when_provider_raises() -> None:
    """Провайдер недоступен (сеть) → degraded, а не отказ."""

    class _BrokenProvider:
        def generate(self, messages: list[dict[str, str]], context: Any = None) -> str:
            raise RuntimeError("connection refused")

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_BrokenProvider())
        draft = await engine.draft_file_content_result(_REQUEST, "owner:session")
        await engine.repository.close()

    assert draft.content is None
    assert draft.status == DRAFT_UNAVAILABLE


# ── 2. Валидный черновик становится содержимым задачи ────────────────────────


@pytest.mark.asyncio
async def test_valid_draft_becomes_task_content() -> None:
    response, backend, _ = await _run_turn(
        FileContentDraft("ANTIGONA FILE TEST\n12345", DRAFT_OK)
    )

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    assert backend.submits[0]["content"] == "ANTIGONA FILE TEST\n12345"
    assert backend.submits[0]["content"] != backend.submits[0]["message"]


# ── 3. Черновик None при ответившем провайдере → fail-closed ─────────────────


@pytest.mark.asyncio
async def test_none_draft_with_responding_provider_is_fail_closed() -> None:
    response, backend, _ = await _run_turn(FileContentDraft(None, DRAFT_REJECTED))

    assert backend.submits == []
    assert response.response_type == ResponseType.CLARIFICATION


# ── 4. Degraded: провайдера нет → прежний fallback сохранён ──────────────────


@pytest.mark.asyncio
async def test_none_draft_without_provider_preserves_fallback() -> None:
    response, backend, _ = await _run_turn(FileContentDraft(None, DRAFT_UNAVAILABLE))

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    # content=None → task_service подставит message (degraded-контракт).
    assert backend.submits[0]["content"] is None


def test_task_service_degraded_fallback_uses_message(tmp_path: Path) -> None:
    """task_service: content=None → в БД попадает текст запроса (degraded)."""
    from antigona.core.task_service import TaskSubmissionService
    from antigona.database import Database
    from antigona.repository import TaskRepository

    database = Database(f"sqlite:///{tmp_path / 'svc.db'}")
    database.create_all()
    service = TaskSubmissionService(database)
    result = service.submit(
        owner_id="owner",
        message=_REQUEST,
        path="manual_test.txt",
        content=None,
        idempotency_key="idem-degraded",
    )
    with database.session_factory() as session:
        task = TaskRepository(session).get(result["id"])
        assert task.content == _REQUEST

    # Явный черновик по-прежнему выигрывает у сообщения.
    result2 = service.submit(
        owner_id="owner",
        message=_REQUEST,
        path="manual_test2.txt",
        content="ANTIGONA FILE TEST\n12345",
        idempotency_key="idem-drafted",
    )
    with database.session_factory() as session:
        task2 = TaskRepository(session).get(result2["id"])
        assert task2.content == "ANTIGONA FILE TEST\n12345"


# ── 5. Регрессия пути записи: на диск попадает ровно task.content ────────────


def test_write_path_writes_exactly_task_content(tmp_path: Path) -> None:
    """orchestrator.py:686-690 не изменён: workspace.write_text пишет task.content."""
    from antigona.contracts import WriteFileInput
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool

    workspace = tmp_path / "ws"
    tool = WorkspaceFileTool(InProcessTestBackend(workspace, test_mode=True), 10)
    expected = "ANTIGONA FILE TEST\n12345"

    result = tool.execute(WriteFileInput(path="manual_test.txt", content=expected))

    assert result.ok, result.error
    written = (workspace / "manual_test.txt").read_text()
    assert written == expected
    assert _REQUEST not in written
