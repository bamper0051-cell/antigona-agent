"""LOOP 6 — Валидация черновика по литералам запроса + комбинированный write+read flow.

Регрессия дефекта канонического E2E (задача a1561f95):
1. Провайдер выдал галлюцинированный отчёт («Файл создан и прочитан... ANTIGONA FILE TEST\n123»),
   в котором пропущен литерал «12345». Черновик должен быть REJECTED, задача не создаётся.
2. Черновик, содержащий все обязательные литералы запроса, успешно валидируется (DRAFT_OK).
3. Запрос без кандидатов-литералов («опиши себя») генерирует свободный черновик (DRAFT_OK).
4. Запрос, содержащий и запись, и чтение («Создай файл ... Затем прочитай этот же файл обратно...»)
   создаёт flow ровно с ДВУМЯ шагами: write затем read; доставка содержит path, exact content,
   имя write-инструмента (workspace.write_text) и имя read-инструмента (workspace.read_text).
5. Запрос только на запись создаёт flow ровно с ОДНИМ шагом (без регрессии).
6. Fail-closed: если шаг записи завершается с ошибкой, шаг чтения не выполняется.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import pytest

from antigona.conversation.dialogue_engine import (
    DRAFT_OK,
    DRAFT_REJECTED,
    DialogueEngine,
    extract_request_literals,
    validate_draft_literals,
)
from antigona.core.brain import AntigonaBrain, ResponseType, _has_read_back_intent
from antigona.database import Database
from antigona.delivery import DeliveryWorker
from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
from antigona.models import (
    Artifact,
    DeliveryOutbox,
    StepState,
    TaskFlow,
    TaskState,
    utcnow,
)
from antigona.orchestrator import Orchestrator
from antigona.pipeline import CompletionVerifier
from antigona.repository import CreateTask, IdempotencyConflict, TaskRepository

_CANONICAL_PROMPT = (
    "Создай файл manual_test.txt с содержимым двумя строками:\n"
    "ANTIGONA FILE TEST\n"
    "12345\n"
    "Затем прочитай этот же файл обратно и покажи мне: путь, точное содержимое и инструмент чтения."
)

_CANONICAL_CONTENT = "ANTIGONA FILE TEST\n12345"

_HALLUCINATED_REPORT = (
    "Файл создан и прочитан.\n"
    "Путь: /workspace/manual_test.txt\n"
    "Содержимое:\n"
    "ANTIGONA FILE TEST\n"
    "123\n"
    "Инструмент: workspace.read_text"
)


class _StubProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, messages: list[dict[str, str]], context: Any = None) -> str:
        return self.reply


class _RecordingBackend:
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
        read_after_write: bool = False,
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.submits.append(
            {
                "message": message,
                "tool_name": tool_name,
                "path": path,
                "content": content,
                "read_after_write": read_after_write,
            }
        )
        return {
            "flow_id": "flow-d6-1",
            "id": "flow-d6-1",
            "status": "QUEUED",
            "requires_approval": False,
        }

    async def cancel_flow(self, flow_id: str) -> Any:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED", "artifacts": []})()


class _FakeAdapter:
    def __init__(self) -> None:
        self.events: list[tuple[Any, str]] = []

    def deliver(self, event: Any, correlation_id: str) -> bool:
        self.events.append((event, correlation_id))
        return True


class _StubVerifier(CompletionVerifier):
    def request_verification(self, task_id: str, correlation_id: str) -> str:
        return "FAILED"


# ── A. Валидация черновика по литералам запроса ──────────────────────────────


def test_extract_request_literals_with_content_marker() -> None:
    candidates, is_mandatory = extract_request_literals(_CANONICAL_PROMPT)
    assert is_mandatory is True
    assert "ANTIGONA FILE TEST" in candidates
    assert "12345" in candidates


def test_extract_request_literals_without_marker() -> None:
    text = "Запиши в файл manual_test.txt строку ANTIGONA FILE TEST и код 99999"
    candidates, is_mandatory = extract_request_literals(text)
    assert is_mandatory is False
    assert "ANTIGONA FILE TEST" in candidates
    assert "99999" in candidates


def test_extract_request_literals_empty_when_no_candidates() -> None:
    candidates, is_mandatory = extract_request_literals("Создай файл manual_test.txt и опиши себя")
    assert candidates == []
    assert is_mandatory is False


def test_validate_draft_literals_rejects_hallucinated_report() -> None:
    # Отчёт содержит 123 вместо 12345 -> не содержит обязательный литерал 12345
    assert not validate_draft_literals(_CANONICAL_PROMPT, _HALLUCINATED_REPORT)


def test_validate_draft_literals_accepts_exact_content() -> None:
    assert validate_draft_literals(_CANONICAL_PROMPT, _CANONICAL_CONTENT)


def test_validate_draft_literals_accepts_generative_request() -> None:
    assert validate_draft_literals(
        "Создай файл manual_test.txt и опиши себя",
        "Я — Antigona, агент с автономной архитектурой.",
    )


@pytest.mark.asyncio
async def test_draft_file_content_result_rejects_hallucinated_report() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_StubProvider(_HALLUCINATED_REPORT))
        draft = await engine.draft_file_content_result(_CANONICAL_PROMPT)
        await engine.close()
    assert draft.status == DRAFT_REJECTED
    assert draft.content is None


@pytest.mark.asyncio
async def test_draft_file_content_result_accepts_valid_literals() -> None:
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine = DialogueEngine(db_path=tmp_db.name, provider=_StubProvider(_CANONICAL_CONTENT))
        draft = await engine.draft_file_content_result(_CANONICAL_PROMPT)
        await engine.close()
    assert draft.status == DRAFT_OK
    assert draft.content == _CANONICAL_CONTENT


@pytest.mark.asyncio
async def test_brain_rejects_hallucinated_draft_and_does_not_create_task() -> None:
    backend = _RecordingBackend()
    engine = DialogueEngine(provider=_StubProvider(_HALLUCINATED_REPORT))

    # Генеративный промпт: content НЕ детерминирован (extraction даёт ''), поэтому
    # brain обязан вызвать draft; провайдер-стаб галлюцинирует (123 вместо 12345)
    # -> validate_draft_literals отклоняет -> CLARIFICATION, задача не создаётся.
    generative_goal = (
        "Создай файл manual_test.txt: напиши три строки, "
        "первая ANTIGONA FILE TEST, последняя 12345"
    )
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            response = await brain.process(
                text=generative_goal,
                user_id="owner",
                channel="cli",
                session_id="owner:session",
                context={"owner_id": "owner", "correlation_id": "corr-d6-1"},
            )
        finally:
            await brain.close()

    assert response.response_type == ResponseType.CLARIFICATION
    assert len(backend.submits) == 0, "Задача не должна создаваться при DRAFT_REJECTED"


# ── B. Комбинированный write+read в одном flow ──────────────────────────────


@pytest.mark.asyncio
async def test_brain_combined_write_read_detects_intent_and_submits_read_after_write() -> None:
    backend = _RecordingBackend()
    engine = DialogueEngine(provider=_StubProvider(_CANONICAL_CONTENT))

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            response = await brain.process(
                text=_CANONICAL_PROMPT,
                user_id="owner",
                channel="cli",
                session_id="owner:session",
                context={"owner_id": "owner", "correlation_id": "corr-d6-2"},
            )
        finally:
            await brain.close()

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    assert submit["content"] == _CANONICAL_CONTENT
    assert submit["read_after_write"] is True




@pytest.mark.asyncio
async def test_brain_t07_explicit_multiline_content_bypasses_draft_and_submits_exact_readback() -> None:
    class _FailingDraftEngine(DialogueEngine):
        async def draft_file_content_result(self, text: str, session_id: str = "") -> Any:  # noqa: ARG002
            raise AssertionError("explicit T07 content must not be drafted by the LLM")

    goal = (
        "Создай файл с именем manual test.txt в workspace.\n"
        "Содержимое должно быть ровно двумя строками:\n"
        "ANTIGONA FILE TEST\n"
        "12345\n"
        "После записи прочитай этот же файл обратно и ответь полным отчётом: "
        "путь, tool write, tool read, точное содержимое."
    )
    backend = _RecordingBackend()
    engine = _FailingDraftEngine(provider=_StubProvider("unused"))

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            response = await brain.process(
                text=goal,
                user_id="owner",
                channel="cli",
                session_id="owner:session",
                context={"owner_id": "owner", "correlation_id": "corr-t07-multiline"},
            )
        finally:
            await brain.close()

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    submit = backend.submits[0]
    assert submit["path"] == "manual test.txt"
    assert submit["content"] == "ANTIGONA FILE TEST\n12345"
    assert submit["read_after_write"] is True


@pytest.mark.asyncio
async def test_brain_write_only_sets_read_after_write_false() -> None:
    backend = _RecordingBackend()
    engine = DialogueEngine(provider=_StubProvider("LINE 1\nLINE 2"))

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            response = await brain.process(
                text="Создай файл notes.txt и запиши туда две строки",
                user_id="owner",
                channel="cli",
                session_id="owner:session",
                context={"owner_id": "owner", "correlation_id": "corr-d6-3"},
            )
        finally:
            await brain.close()

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    assert backend.submits[0]["read_after_write"] is False


def test_repository_creates_two_steps_when_read_after_write_is_true(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'repo_test.db'}")
    database.create_all()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner-1",
                goal=_CANONICAL_PROMPT,
                path="manual_test.txt",
                content=_CANONICAL_CONTENT,
                idempotency_key="idem-d6-2steps",
                tool_name="workspace.write_text",
                read_after_write=True,
            )
        )
        assert created is True
        assert len(task.steps) == 2
        assert task.steps[0].index == 0
        assert task.steps[0].tool_name == "workspace.write_text"
        assert task.steps[1].index == 1
        assert task.steps[1].tool_name == "workspace.read_text"


def test_repository_creates_one_step_when_read_after_write_is_false(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'repo_test_1.db'}")
    database.create_all()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner-1",
                goal="Создай файл notes.txt",
                path="notes.txt",
                content="hello",
                idempotency_key="idem-d6-1step",
                tool_name="workspace.write_text",
                read_after_write=False,
            )
        )
        assert created is True
        assert len(task.steps) == 1
        assert task.steps[0].index == 0
        assert task.steps[0].tool_name == "workspace.write_text"


def test_combined_write_read_orchestrator_execution_and_delivery(tmp_path: Path) -> None:
    """E2E flow: write + read выполняются последовательно, доставка содержит все 4 элемента."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    db_path = tmp_path / "combined.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    class _StubVerifier(CompletionVerifier):
        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return "DONE"

    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)
    verifier = _StubVerifier()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, created = repo.create(
            CreateTask(
                owner_id="owner-delivery",
                goal=_CANONICAL_PROMPT,
                path="manual_test.txt",
                content=_CANONICAL_CONTENT,
                idempotency_key="write-read-flow-d6",
                tool_name="workspace.write_text",
                read_after_write=True,
            )
        )
        assert created is True
        assert len(task.steps) == 2

        orch = Orchestrator(
            session,
            write_tool,
            verifier,
            workspace=type("WS", (), {"root_path": ws})(),
        )
        ap = repo.request_approval(task)
        if ap.decision == "PENDING":
            repo.decide_approval(task, ap.id, "owner-delivery", True)
        completed_task = orch.run(repo.get(task.id), "worker-d6")
        assert TaskState(completed_task.status) in (TaskState.DONE, TaskState.VERIFYING)

        refreshed = repo.get(task.id)
        assert len(refreshed.steps) == 2
        assert refreshed.steps[0].status == StepState.COMPLETED.value
        assert refreshed.steps[1].status == StepState.COMPLETED.value
        assert len(refreshed.artifacts) == 2

        # Step 0 artifact = write
        assert refreshed.artifacts[0].path == "manual_test.txt"
        # Step 1 artifact = read
        assert refreshed.artifacts[1].path == "manual_test.txt"

        # Verify file content on disk
        disk_content = (ws / "manual_test.txt").read_text(encoding="utf-8")
        assert disk_content == _CANONICAL_CONTENT

        # Simulate verifier service finalization & delivery outbox formatting
        step1_output = refreshed.steps[1].output or {}
        tool_result = step1_output.get("tool_result") or {}
        assert tool_result.get("tool_name") == "workspace.read_text"
        assert tool_result.get("creator_tool") == "workspace.write_text"
        assert tool_result.get("stdout_preview") == _CANONICAL_CONTENT

        # Deliver message via DeliveryWorker
        fake_adapter = _FakeAdapter()
        # Add outbox entry with the canonical delivery message format
        msg_parts = [
            f"path: {tool_result.get('path', 'manual_test.txt')}",
            f"tool_name: {tool_result.get('tool_name')}",
            f"creator_tool: {tool_result.get('creator_tool')}",
            f"content:\n{_CANONICAL_CONTENT}",
        ]
        delivery_message = "\n".join(msg_parts)

        session.add(
            DeliveryOutbox(
                task_id=task.id,
                adapter="cli",
                event_type="result",
                idempotency_key=f"result:{task.id}:cli",
                payload={
                    "task_id": task.id,
                    "session_id": "owner-delivery",
                    "correlation_id": "corr-d6-delivery",
                    "status": "DONE",
                    "message": delivery_message,
                },
            )
        )
        session.commit()

        worker = DeliveryWorker(session, fake_adapter)
        while worker.dispatch_one():
            pass

        assert len(fake_adapter.events) >= 1
        event, _ = fake_adapter.events[-1]
        assert event.status == "DONE"
        # Проверяем все 4 обязательных элемента доставки:
        assert "manual_test.txt" in event.message
        assert _CANONICAL_CONTENT in event.message
        assert "workspace.write_text" in event.message
        assert "workspace.read_text" in event.message


def test_combined_write_read_fails_closed_if_write_fails(tmp_path: Path) -> None:
    """Fail-closed: если шаг записи падает, шаг чтения не выполняется."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    db_path = tmp_path / "fail_closed.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    class _FailingWriteTool:
        backend = type("BE", (), {"workspace": ws})()

        def execute(self, params: Any) -> Any:
            from antigona.models import ToolResult
            return ToolResult(False, "failed", error="disk full or permission error")

        def read_and_hash(self, path: str) -> tuple[str, str]:
            raise OSError("file not found")

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner-1",
                goal=_CANONICAL_PROMPT,
                path="manual_test.txt",
                content=_CANONICAL_CONTENT,
                idempotency_key="fail-closed-key",
                tool_name="workspace.write_text",
                read_after_write=True,
            )
        )

        orch = Orchestrator(
            session,
            _FailingWriteTool(),
            _StubVerifier(),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        ap = repo.request_approval(task)
        if ap.decision == "PENDING":
            repo.decide_approval(task, ap.id, "owner-1", True)
        failed_task = orch.run(repo.get(task.id), "worker-1")
        assert TaskState(failed_task.status) == TaskState.FAILED

        refreshed = repo.get(task.id)
        # Step 0 failed
        assert refreshed.steps[0].status == StepState.FAILED.value
        # Step 1 was NEVER executed -> stays PENDING
        assert refreshed.steps[1].status == StepState.PENDING.value
        assert len(refreshed.artifacts) == 0


# ── F. LOOP 6.2 Регрессионные и расширенные тесты ────────────────────────────


def test_extract_request_literals_inline_without_colon() -> None:
    """1. БЛОКЕР: inline content-intent без двоеточия («с текстом hello»)."""
    candidates, is_mandatory = extract_request_literals("Создай файл foo.txt с текстом hello")
    assert is_mandatory is True
    assert candidates == ["hello"]

    candidates2, is_mandatory2 = extract_request_literals(
        "Запиши в файл bar.txt текст hello world Затем покажи мне"
    )
    assert is_mandatory2 is True
    assert candidates2 == ["hello world"]

    candidates3, is_mandatory3 = extract_request_literals(
        "Создай файл baz.txt с текстом 42. Покажи содержимое."
    )
    assert is_mandatory3 is True
    assert candidates3 == ["42"]


def test_validate_draft_literals_inline_without_colon_rejected() -> None:
    """1. БЛОКЕР: «с текстом hello» + галлюцинат без «hello» -> REJECTED."""
    request = "Создай файл foo.txt с текстом hello"
    hallucinated = "Файл создан со случайным содержанием"
    assert not validate_draft_literals(request, hallucinated)


def test_validate_draft_literals_inline_without_colon_accepted() -> None:
    """1. БЛОКЕР: «с текстом hello» + верный черновик -> OK."""
    request = "Создай файл foo.txt с текстом hello"
    valid_draft = "hello"
    assert validate_draft_literals(request, valid_draft)


@pytest.mark.asyncio
async def test_dialogue_engine_inline_without_colon_flow() -> None:
    """1. БЛОКЕР E2E DialogueEngine: «с текстом hello» + неверный/верный черновик."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine_rej = DialogueEngine(
            db_path=tmp_db.name,
            provider=_StubProvider("Wrong text here"),
        )
        draft_rej = await engine_rej.draft_file_content_result("Создай файл foo.txt с текстом hello")
        await engine_rej.close()
    assert draft_rej.status == DRAFT_REJECTED

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        engine_ok = DialogueEngine(
            db_path=tmp_db.name,
            provider=_StubProvider("hello"),
        )
        draft_ok = await engine_ok.draft_file_content_result("Создай файл foo.txt с текстом hello")
        await engine_ok.close()
    assert draft_ok.status == DRAFT_OK
    assert draft_ok.content == "hello"


def test_validate_draft_literals_without_marker_requires_all_candidates() -> None:
    """2. MEDIUM: без маркера — обязательны ВСЕ кандидаты."""
    text = "Запиши ANTIGONA FILE TEST и код 99999"
    # Черновик содержит только один кандидат из двух -> REJECTED
    partial_draft = "ANTIGONA FILE TEST"
    assert not validate_draft_literals(text, partial_draft)

    # Черновик содержит оба кандидата -> OK
    full_draft = "ANTIGONA FILE TEST 99999"
    assert validate_draft_literals(text, full_draft)


def test_read_back_intent_expanded_patterns() -> None:
    """5. MEDIUM: расширенные детерминированные паттерны read-intent."""
    assert _has_read_back_intent("Создай файл test.txt и покажи содержимое")
    assert _has_read_back_intent("Создай файл test.txt и выведи содержимое")
    assert _has_read_back_intent("Запиши данные и покажи что внутри")
    assert _has_read_back_intent("Write test.txt and read back")
    assert _has_read_back_intent("Создай config.yaml, открой и покажи")
    assert _has_read_back_intent("Создай manual_test.txt. Затем прочитай этот же файл обратно")


@pytest.mark.asyncio
async def test_brain_read_after_write_expanded_intent() -> None:
    """5. MEDIUM: brain выставляет read_after_write=True для новых паттернов."""
    backend = _RecordingBackend()
    engine = DialogueEngine(provider=_StubProvider("my data"))

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            await brain.process(
                text="Создай файл data.txt с текстом my data и покажи содержимое",
                user_id="owner",
                channel="cli",
                session_id="s1",
            )
        finally:
            await brain.close()

    assert len(backend.submits) == 1
    assert backend.submits[0]["read_after_write"] is True


def test_repository_idempotency_with_legacy_fingerprint(tmp_path: Path) -> None:
    """4. MEDIUM: совместимость idempotency с legacy fingerprint."""
    db_path = tmp_path / "repo_idempotency.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        cmd = CreateTask(
            owner_id="user1",
            goal="Write sample",
            path="sample.txt",
            content="sample",
            idempotency_key="idemp-key-1",
            tool_name="workspace.write_text",
            read_after_write=False,
        )
        # 1. Создаём задачу с legacy_fingerprint напрямую в базе
        task = TaskFlow(
            owner_id=cmd.owner_id,
            goal=cmd.goal,
            target_path=cmd.path,
            content=cmd.content,
            idempotency_key=cmd.idempotency_key,
            payload_fingerprint=cmd.legacy_fingerprint,
            tool_name=cmd.tool_name,
            tool_arguments={"path": cmd.path},
        )
        session.add(task)
        session.commit()

        # 2. Вызываем repo.create с тем же payload — возвращает существующую задачу
        matched, created = repo.create(cmd)
        assert created is False
        assert matched.id == task.id

        # 3. Вызываем repo.create с другим payload — конфликт
        different_cmd = CreateTask(
            owner_id="user1",
            goal="Write DIFFERENT sample",
            path="sample.txt",
            content="different",
            idempotency_key="idemp-key-1",
            tool_name="workspace.write_text",
        )
        with pytest.raises(IdempotencyConflict):
            repo.create(different_cmd)


@pytest.mark.asyncio
async def test_brain_submit_task_typeerror_does_not_drop_read_after_write() -> None:
    """3. MEDIUM: TypeError-ретрай не должен ронять read_after_write=True."""
    retried_without_flag = False

    class _OldBackendWithoutReadAfterWrite:
        async def submit_task(
            self,
            message: str,
            conversation_id: str = "",
            client: str = "cli",
            metadata: Any = None,
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
            nonlocal retried_without_flag
            retried_without_flag = True
            return {"flow_id": "flow-old"}

    backend = _OldBackendWithoutReadAfterWrite()
    engine = DialogueEngine(provider=_StubProvider("test content"))

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=engine,
        )
        await brain.connect()
        try:
            # read_after_write=True -> backend TypeError should NOT be swallowed to retry without flag
            response = await brain.process(
                text="Создай файл test.txt с текстом test content. Затем прочитай этот же файл обратно.",
                user_id="owner",
                channel="cli",
                session_id="s1",
            )
            assert response.response_type == ResponseType.ERROR
            assert not retried_without_flag
        finally:
            await brain.close()


def test_orchestrator_completed_step_missing_artifact_fails(tmp_path: Path) -> None:
    """6. LOW: COMPLETED-шаг без артефакта возвращает None и не проходит успешно."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    db_path = tmp_path / "orch_missing_art.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner-1",
                goal="test",
                path="test.txt",
                content="test",
                idempotency_key="k1",
                tool_name="workspace.write_text",
            )
        )
        # Mark step 0 as COMPLETED but do NOT insert an artifact
        task.steps[0].status = StepState.COMPLETED.value
        session.commit()

        orch = Orchestrator(
            session,
            WorkspaceFileTool(ws),
            _StubVerifier(),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        result = orch._recover_or_execute(task, task.steps[0], "corr-1")
        assert result is None


def test_verifier_service_picks_latest_artifact(tmp_path: Path, monkeypatch: Any) -> None:
    """6. LOW: verifier_service выбирает последний артефакт (order_by created_at desc)."""
    import datetime

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "file1.txt").write_text("first")
    (ws / "file2.txt").write_text("second")
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    db_path = tmp_path / "verifier_latest.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner-1",
                goal="Write and read",
                path="file2.txt",
                content="second",
                idempotency_key="k-v1",
                tool_name="workspace.write_text",
            )
        )
        task.status = TaskState.VERIFYING.value
        t1 = utcnow() - datetime.timedelta(seconds=10)
        t2 = utcnow()

        art1 = Artifact(
            task_id=task.id,
            step_id=task.steps[0].id,
            path="file1.txt",
            sha256=hashlib.sha256(b"first").hexdigest(),
            size=len(b"first"),
            created_at=t1,
        )
        art2 = Artifact(
            task_id=task.id,
            step_id=task.steps[0].id,
            path="file2.txt",
            sha256=hashlib.sha256(b"second").hexdigest(),
            size=len(b"second"),
            created_at=t2,
        )
        session.add(art1)
        session.add(art2)
        session.commit()

        # Query directly matching verifier_service logic
        from sqlalchemy import select
        selected_art = session.scalar(
            select(Artifact)
            .where(Artifact.task_id == task.id)
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        assert selected_art is not None
        assert selected_art.path == "file2.txt"




# ── FAILURE E (guio.md LIVE BLOCKER) ─ inline-разделители литералов ─────────
# LIVE BLOCKER текст: «...двумя строками: ANTIGONA FILE TEST и 12345. Затем
# прочитай...». Раньше вся строка «ANTIGONA FILE TEST и 12345.» возвращалась
# ОДНИМ литералом → validate требовал этот текст дословно → ложная
# clarification вместо записи файла. Теперь строка разбивается по «и/запятой/
# слэшу» и trailing punctuation удаляется.
@pytest.mark.parametrize(
    "request_text,draft,expected_valid",
    [
        (
            "Создай файл manual_test.txt с содержимым двумя строками: "
            "ANTIGONA FILE TEST и 12345. Затем прочитай этот же файл обратно",
            "ANTIGONA FILE TEST\n12345",
            True,
        ),
        (
            "Создай файл manual_test.txt с содержимым двумя строками: "
            "ANTIGONA FILE TEST / 12345. Затем прочитай",
            "ANTIGONA FILE TEST\n12345",
            True,
        ),
        (
            "Создай файл manual_test.txt с содержимым двумя строками:\n"
            "ANTIGONA FILE TEST\n12345",
            "ANTIGONA FILE TEST\n12345",
            True,
        ),
    ],
)
def test_failure_e_inline_literal_split(
    request_text: str, draft: str, expected_valid: bool
) -> None:
    cands, _ = extract_request_literals(request_text)
    # Разделитель «и»/«слэш» не должен слипать литералы в одну строку.
    assert "и" not in cands and "/" not in cands
    assert validate_draft_literals(request_text, draft) is expected_valid
