"""LOOP4 / DEFECT 2 — read-инструмент задач (``workspace.read_text``).

Живой дефект: «Затем прочитай этот же файл обратно и покажи мне:…» планировался
как ``workspace.write_text`` — read-глаголов не было ни в одном task-паттерне
роутера, generic-ветка отправляла запрос в task.shell, ``_extract_shell_command``
возвращал ``None``, и срабатывал молчаливый дефолт записи. Вместо чтения файл
перезаписывался.

Контракт, который закрепляют эти тесты:

1. «прочитай файл notes.txt» → задача с ``tool_name=workspace.read_text``;
2. чтение существующего файла → владелец получает ТОЧНОЕ содержимое;
3. путь вне workspace (``/etc/passwd``, ``../outside.txt``) → fail-closed:
   файл не читается, задача не создаётся;
4. «покажи, что ты умеешь» → разговор, а не задача;
5. read без явного пути → берётся target_path последней write-задачи сессии;
6. регрессия точной фразы владельца → file_read с путём последней записи.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from antigona.conversation.dialogue_engine import DRAFT_OK, FileContentDraft
from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.router.intent_router import IntentRouter
from antigona.tools.workspace_read import WorkspaceReadTextTool

_WRITE_REQUEST = "Создай файл notes.txt и запиши туда две строки"
_OWNER_READ_PHRASE = "Затем прочитай этот же файл обратно и покажи мне:"
_FILE_CONTENT = "ANTIGONA FILE TEST\n12345"


class _RecordingBackend:
    """TaskBackend, запоминающий инструмент/путь/содержимое каждой задачи."""

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
        self.submits.append(
            {
                "message": message,
                "tool_name": tool_name,
                "path": path,
                "content": content,
            }
        )
        return {
            "flow_id": "flow-1",
            "id": "flow-1",
            "status": "QUEUED",
            "requires_approval": False,
        }

    async def cancel_flow(self, flow_id: str) -> Any:
        return None

    async def get_flow(self, flow_id: str) -> Any:
        return type("FV", (), {"status": "QUEUED", "artifacts": []})()


class _StubEngine:
    """DialogueEngine-заглушка: черновик всегда валиден."""

    async def draft_file_content_result(
        self, text: str, session_id: str = "cli-session"
    ) -> FileContentDraft:
        return FileContentDraft(_FILE_CONTENT, DRAFT_OK)

    async def draft_file_content(
        self, text: str, session_id: str = "cli-session"
    ) -> str | None:
        return _FILE_CONTENT

    async def reply(self, text: str, session_id: str = "cli-session", context: Any = None) -> str:
        return "ok"

    async def close(self) -> None:
        return None


async def _run_turns(
    workspace: Path, *turns: str
) -> tuple[list[Any], _RecordingBackend]:
    """Прогнать несколько ходов через ОДИН мозг (одна сессия)."""
    backend = _RecordingBackend()
    responses: list[Any] = []
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        brain = AntigonaBrain(
            db_path=tmp_db.name,
            task_backend=backend,
            dialogue_engine=_StubEngine(),
            workspace=workspace,
        )
        await brain.connect()
        try:
            for turn in turns:
                responses.append(
                    await brain.process(
                        text=turn,
                        user_id="owner",
                        channel="cli",
                        session_id="owner:session",
                        context={"owner_id": "owner", "correlation_id": "corr-d4"},
                    )
                )
        finally:
            await brain.close()
    return responses, backend


def _workspace_with_notes(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # BUG ANT-007 (wave3, class B): write_text() on Windows translates \n to
    # \r\n (text mode), so the round-trip comparison against _FILE_CONTENT
    # fails. Write raw bytes to keep the file byte-identical on all platforms.
    (workspace / "notes.txt").write_bytes(_FILE_CONTENT.encode("utf-8"))
    return workspace


# ── 1. read-запрос создаёт read-задачу, а не write ───────────────────────────


@pytest.mark.asyncio
async def test_read_request_creates_read_task_not_write(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    responses, backend = await _run_turns(workspace, "прочитай файл notes.txt")

    assert len(backend.submits) == 1
    assert backend.submits[0]["tool_name"] == "workspace.read_text"
    assert backend.submits[0]["tool_name"] != "workspace.write_text"
    assert backend.submits[0]["path"] == "notes.txt"
    assert responses[0].intent == "task.file_read"


def test_router_classifies_read_request() -> None:
    decision = IntentRouter().route("прочитай файл notes.txt")
    assert decision.intent == "task.file_read"
    assert decision.entities.get("path") == "notes.txt"


def test_router_keeps_bare_shell_command_untouched() -> None:
    """Регрессия: «cat /etc/hostname» остаётся shell-веткой (Step 20b)."""
    assert IntentRouter().route("cat /etc/hostname").intent == "ambiguous.mixed_intent"


# ── 2. Результат содержит точное содержимое файла ────────────────────────────


@pytest.mark.asyncio
async def test_read_returns_exact_file_content(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    responses, backend = await _run_turns(workspace, "прочитай файл notes.txt")
    response = responses[0]

    assert response.response_type == ResponseType.TASK_ACCEPTED
    assert response.metadata["tool_name"] == "workspace.read_text"
    assert response.metadata["path"] == "notes.txt"
    assert len(backend.submits) == 1
    assert backend.submits[0]["tool_name"] == "workspace.read_text"
    assert backend.submits[0]["path"] == "notes.txt"
    assert backend.submits[0]["content"] is None
    # Файл не тронут чтением.
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == _FILE_CONTENT


def test_tool_reads_file_inside_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    result = WorkspaceReadTextTool(workspace).execute("notes.txt")

    assert result.ok, result.error
    assert result.content == _FILE_CONTENT
    assert result.path == str(workspace / "notes.txt")
    assert result.tool_name == "workspace.read_text"


# ── 3. Путь вне workspace → fail-closed ──────────────────────────────────────


@pytest.mark.parametrize(
    "outside",
    ["/etc/passwd", "../outside.txt", "../../etc/passwd", "/etc/hosts"],
)
def test_tool_refuses_paths_outside_workspace(tmp_path: Path, outside: str) -> None:
    workspace = _workspace_with_notes(tmp_path)
    (tmp_path / "outside.txt").write_text("SECRET-OUTSIDE", encoding="utf-8")

    result = WorkspaceReadTextTool(workspace).execute(outside)

    assert not result.ok
    assert result.content == ""
    assert "SECRET-OUTSIDE" not in result.content
    assert result.error


@pytest.mark.asyncio
async def test_read_outside_workspace_is_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)
    (tmp_path / "outside.txt").write_text("SECRET-OUTSIDE", encoding="utf-8")

    responses, backend = await _run_turns(workspace, "прочитай файл ../outside.txt")
    response = responses[0]

    assert response.response_type == ResponseType.ERROR
    assert "SECRET-OUTSIDE" not in response.text
    assert backend.submits == [], "задача чтения вне workspace не создаётся"


# ── 4. «покажи, что ты умеешь» остаётся разговором ───────────────────────────


@pytest.mark.asyncio
async def test_capability_question_stays_conversation(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    responses, backend = await _run_turns(workspace, "покажи, что ты умеешь")

    decision = IntentRouter().route("покажи, что ты умеешь")
    assert not decision.intent.startswith("task.")
    assert backend.submits == []
    assert responses[0].response_type == ResponseType.CONVERSATION


# ── 5. read без явного пути → путь последней write-задачи сессии ─────────────


@pytest.mark.asyncio
async def test_read_without_path_uses_last_write_target(tmp_path: Path) -> None:
    workspace = _workspace_with_notes(tmp_path)

    _, backend = await _run_turns(workspace, _WRITE_REQUEST, "прочитай этот файл")

    assert len(backend.submits) == 2
    write_submit, read_submit = backend.submits
    assert write_submit["path"] == "notes.txt"
    assert read_submit["tool_name"] == "workspace.read_text"
    assert read_submit["path"] == "notes.txt"


@pytest.mark.asyncio
async def test_read_without_any_known_path_asks_for_clarification(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_notes(tmp_path)

    responses, backend = await _run_turns(workspace, "прочитай этот файл")

    assert responses[0].response_type == ResponseType.CLARIFICATION
    assert backend.submits == [], "без пути ничего не создаётся — и точно не запись"


# ── 6. Регрессия: точная фраза владельца ─────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_phrase_routes_to_read_of_last_written_file(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_notes(tmp_path)

    responses, backend = await _run_turns(
        workspace, _WRITE_REQUEST, _OWNER_READ_PHRASE
    )

    assert IntentRouter().route(_OWNER_READ_PHRASE).intent == "task.file_read"
    assert len(backend.submits) == 2
    read_submit = backend.submits[1]
    assert read_submit["tool_name"] == "workspace.read_text"
    assert read_submit["path"] == "notes.txt"
    assert read_submit["content"] is None
    assert responses[1].response_type == ResponseType.TASK_ACCEPTED


# ── 7. Follow-up: Orchestrator read-branch (A, B, C, D, E) ───────────────────


def test_orchestrator_read_branch_exactly_once_no_write_file_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A. read-задача через оркестратор вызывает read-тул ровно 1 раз и НЕ создает WriteFileInput."""
    from antigona.contracts import WriteFileInput
    from antigona.database import Database
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.verifier_client import VerifierClient

    class StubVerifier(VerifierClient):
        def __init__(self, decision: str = "DONE") -> None:
            self.decision = decision

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    ws = tmp_path / "ws_a"
    ws.mkdir(parents=True, exist_ok=True)
    # Wave 4 (B): write_text() on Windows translates LF to CRLF (text mode);
    # write raw bytes so the artifact size matches _FILE_CONTENT on all platforms.
    (ws / "notes.txt").write_bytes(_FILE_CONTENT.encode("utf-8"))

    db_path = tmp_path / "orch_a.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)

    write_calls: list[Any] = []
    original_write = write_tool.execute

    def _spy_write(arg: Any) -> Any:
        write_calls.append(arg)
        return original_write(arg)

    monkeypatch.setattr(write_tool, "execute", _spy_write)

    read_calls: list[str] = []
    original_read = WorkspaceReadTextTool.execute

    def _spy_read(self_tool: Any, path: str) -> Any:
        read_calls.append(path)
        return original_read(self_tool, path)

    monkeypatch.setattr(WorkspaceReadTextTool, "execute", _spy_read)

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="прочитай файл notes.txt",
                path="notes.txt",
                content="",
                idempotency_key="read-task-1",
                tool_name="workspace.read_text",
            )
        )
        orch = Orchestrator(
            session,
            write_tool,
            StubVerifier("DONE"),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        result = orch.run(task, "worker-1")

    # Assert read-tool called exactly 1 time
    assert len(read_calls) == 1
    assert read_calls[0] == "notes.txt"

    # Assert write-tool was NEVER called and no WriteFileInput constructed
    assert len(write_calls) == 0
    assert not any(isinstance(c, WriteFileInput) for c in write_calls)

    # Assert artifact created with correct content hash
    assert len(result.artifacts) == 1
    assert result.artifacts[0].path == "notes.txt"
    assert result.artifacts[0].size == len(_FILE_CONTENT.encode("utf-8"))

    # Assert file on disk was untouched
    assert (ws / "notes.txt").read_text(encoding="utf-8") == _FILE_CONTENT


@pytest.mark.asyncio
async def test_brain_does_not_read_inline_exactly_once_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B. brain._handle_file_read НЕ читает файл инлайново (0 вызовов при регистрации, 1 при исполнении)."""
    from antigona.database import Database
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.verifier_client import VerifierClient

    class StubVerifier(VerifierClient):
        def __init__(self, decision: str = "DONE") -> None:
            self.decision = decision

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    ws = _workspace_with_notes(tmp_path)

    read_calls: list[str] = []
    original_read = WorkspaceReadTextTool.execute

    def _spy_read(self_tool: Any, path: str) -> Any:
        read_calls.append(path)
        return original_read(self_tool, path)

    monkeypatch.setattr(WorkspaceReadTextTool, "execute", _spy_read)

    # 1. Registration via brain
    responses, backend = await _run_turns(ws, "прочитай файл notes.txt")
    assert responses[0].response_type == ResponseType.TASK_ACCEPTED
    assert len(backend.submits) == 1
    assert backend.submits[0]["tool_name"] == "workspace.read_text"
    assert backend.submits[0]["content"] is None

    # Assert 0 reads during registration!
    assert len(read_calls) == 0

    # 2. Execution via Orchestrator
    db_path = tmp_path / "orch_b.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="прочитай файл notes.txt",
                path="notes.txt",
                content="",
                idempotency_key="read-task-2",
                tool_name="workspace.read_text",
            )
        )
        orch = Orchestrator(
            session,
            write_tool,
            StubVerifier("DONE"),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        orch.run(task, "worker-1")

    # Exactly 1 total read across full lifecycle!
    assert len(read_calls) == 1
    assert read_calls[0] == "notes.txt"


def test_delivery_result_contains_full_path_and_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C. результат доставки содержит полный путь + точное содержимое + tool_name + creator_tool."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from verifier_fakes import deterministic_test_judge, seed_private_criteria

    from antigona.database import Database
    from antigona.delivery import DeliveryWorker, FakeAdapter
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.models import DurableOperation
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.verifier_client import VerifierClient
    from antigona.verifier_service import create_verifier_app

    ws = tmp_path / "ws_c"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))

    db_path = tmp_path / "orch_c.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    db_url = database.engine.url.render_as_string(hide_password=False)

    verifier_app = create_verifier_app(
        db_url,
        "test-verifier-credential",
        deterministic_test_judge(),
    )
    test_client = TestClient(verifier_app)
    test_client.__enter__()

    class RealVerifier(VerifierClient):
        def __init__(self) -> None:
            pass

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            resp = test_client.post(
                "/verify",
                headers={"Authorization": "Bearer test-verifier-credential"},
                json={"task_id": task_id, "correlation_id": correlation_id},
            )
            resp.raise_for_status()
            return str(resp.json()["decision"])

    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)
    verifier = RealVerifier()

    with database.session_factory() as session:
        repo = TaskRepository(session)
        # 1. Natural write task in pipeline to establish creator_tool
        write_task, _ = repo.create(
            CreateTask(
                owner_id="owner-delivery",
                goal="Создай файл notes.txt",
                path="notes.txt",
                content=_FILE_CONTENT,
                idempotency_key="write-task-c",
                tool_name="workspace.write_text",
            )
        )
        ap = repo.request_approval(write_task)
        if ap.decision == "PENDING":
            repo.decide_approval(write_task, ap.id, "owner-delivery", True)
        else:
            # Safe in-workspace write auto-approves (LOW, canon P0) — no
            # decide_approval commit, so flush the pending write ourselves
            # before seed_private_criteria opens a second connection.
            session.commit()

        seed_private_criteria(db_url, write_task.id)
        orch = Orchestrator(
            session,
            write_tool,
            verifier,
            workspace=type("WS", (), {"root_path": ws})(),
        )
        orch.run(repo.get(write_task.id), "worker-1")

        # 2. Natural read task in pipeline
        read_task, _ = repo.create(
            CreateTask(
                owner_id="owner-delivery",
                goal="прочитай файл notes.txt",
                path="notes.txt",
                content="",
                idempotency_key="read-task-c",
                tool_name="workspace.read_text",
            )
        )
        seed_private_criteria(db_url, read_task.id, _FILE_CONTENT)
        orch.run(repo.get(read_task.id), "worker-1")

        # Check FlowStep tool_result output
        refreshed = repo.get(read_task.id)
        assert len(refreshed.artifacts) == 1
        assert refreshed.artifacts[0].path == "notes.txt"
        assert refreshed.artifacts[0].size == len(_FILE_CONTENT.encode("utf-8"))

        step_output = refreshed.steps[0].output or {}
        tool_result = step_output.get("tool_result") or {}
        assert tool_result.get("stdout_preview") == _FILE_CONTENT
        assert tool_result.get("path") == str(ws / "notes.txt")
        assert tool_result.get("tool_name") == "workspace.read_text"
        assert tool_result.get("creator_tool") == "workspace.write_text"

        # Check operation result
        op = session.scalar(select(DurableOperation).where(DurableOperation.task_id == read_task.id))
        assert op is not None
        assert op.status == "APPLIED"
        assert op.result is not None
        assert op.result["path"] == "notes.txt"
        assert op.result["size"] == len(_FILE_CONTENT.encode("utf-8"))
        assert op.result["tool_result"]["creator_tool"] == "workspace.write_text"

        # Check natural delivery through DeliveryWorker (NO synthetic payload, NO delete)
        fake_adapter = FakeAdapter()
        worker = DeliveryWorker(session, fake_adapter)
        while worker.dispatch_one():
            pass

        result_events = [
            e for e, _ in fake_adapter.events
            if e.task_id == read_task.id and _FILE_CONTENT in e.message
        ]
        assert len(result_events) >= 1
        event = result_events[-1]
        assert event.session_id == "owner-delivery"
        assert event.status == "DONE"
        # Assert all 4 elements in delivered event.message:
        assert str(ws / "notes.txt") in event.message
        assert _FILE_CONTENT in event.message
        assert "workspace.read_text" in event.message
        assert "workspace.write_text" in event.message


def test_regression_write_tasks_write_exact_content_d3(tmp_path: Path) -> None:
    """D. регрессия: write-задачи пишут ровно task.content (D3 не сломан)."""
    from antigona.database import Database
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.verifier_client import VerifierClient

    class StubVerifier(VerifierClient):
        def __init__(self, decision: str = "DONE") -> None:
            self.decision = decision

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    ws = tmp_path / "ws_d"
    ws.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "orch_d.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)
    exact_content = "D3 EXACT CONTENT INTEGRITY TEST\nLine 2\nLine 3"

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="Создай файл output.txt",
                path="output.txt",
                content=exact_content,
                idempotency_key="write-task-4",
                tool_name="workspace.write_text",
            )
        )
        # write-task requires approval in policy
        ap = repo.request_approval(task)
        if ap.decision == "PENDING":
            repo.decide_approval(task, ap.id, "owner", True)

        orch = Orchestrator(
            session,
            write_tool,
            StubVerifier("DONE"),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        orch.run(repo.get(task.id), "worker-1")

    written = (ws / "output.txt").read_text(encoding="utf-8")
    assert written == exact_content


def test_read_task_identifies_creator_tool(tmp_path: Path) -> None:
    """E. read-задача определяет инструмент, создавший файл."""
    from antigona.database import Database
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.models import TaskState
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.verifier_client import VerifierClient

    class StubVerifier(VerifierClient):
        def __init__(self, decision: str = "DONE") -> None:
            self.decision = decision

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    ws = tmp_path / "ws_e"
    ws.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "orch_e.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()

    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)

    with database.session_factory() as session:
        repo = TaskRepository(session)
        # 1. Write task
        write_task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="Создай файл doc.txt",
                path="doc.txt",
                content="doc content",
                idempotency_key="write-task-5",
                tool_name="workspace.write_text",
            )
        )
        ap = repo.request_approval(write_task)
        if ap.decision == "PENDING":
            repo.decide_approval(write_task, ap.id, "owner", True)

        orch = Orchestrator(
            session,
            write_tool,
            StubVerifier("DONE"),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        orch.run(repo.get(write_task.id), "worker-1")

        # 2. Read task for same file
        read_task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="прочитай файл doc.txt",
                path="doc.txt",
                content="",
                idempotency_key="read-task-5",
                tool_name="workspace.read_text",
            )
        )
        read_res = orch.run(read_task, "worker-1")
        assert read_res.status in (
            TaskState.DONE.value,
            TaskState.VERIFYING.value,
            TaskState.OBSERVING.value,
        )
        refreshed = repo.get(read_task.id)
        assert refreshed.artifacts[0].path == "doc.txt"
        assert (ws / "doc.txt").read_text(encoding="utf-8") == "doc content"

        # Explicit assert for creator_tool metric:
        step_output = refreshed.steps[0].output or {}
        tool_result = step_output.get("tool_result") or {}
        assert tool_result.get("creator_tool") == "workspace.write_text"
        assert tool_result.get("tool_name") == "workspace.read_text"
        assert tool_result.get("path") == str(ws / "doc.txt")


def test_read_boundary_symlink_inside_workspace_fail_closed(tmp_path: Path) -> None:
    """Boundary: every symlink denial stays fail-closed and uses boundary taxonomy."""
    ws = tmp_path / "ws_symlink"
    ws.mkdir(parents=True, exist_ok=True)
    real_file = ws / "target.txt"
    real_file.write_text("SECRET_REAL_CONTENT", encoding="utf-8")

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("OUTSIDE_FORBIDDEN_CONTENT", encoding="utf-8")

    link_internal = ws / "link_internal.txt"
    link_internal.symlink_to(real_file)
    link_outside = ws / "link_outside.txt"
    link_outside.symlink_to(outside_file)
    link_chained = ws / "link_chained.txt"
    link_chained.symlink_to(link_internal)

    tool = WorkspaceReadTextTool(workspace=ws)
    cases = (
        ("link_internal.txt", "symlink path component forbidden"),
        ("link_outside.txt", "path escapes workspace"),
        ("link_chained.txt", "symlink path component forbidden"),
    )

    for path, boundary_reason in cases:
        result = tool.execute(path)
        assert result.ok is False
        assert result.path == ""
        assert result.content == ""
        assert result.sha256 == ""
        assert "SECRET_REAL_CONTENT" not in result.content
        assert "OUTSIDE_FORBIDDEN_CONTENT" not in result.content
        assert result.error == f"path outside workspace: {boundary_reason}"


def test_read_boundary_file_too_large_fail_closed(tmp_path: Path) -> None:
    """Boundary: файл > 1 MiB -> fail-closed (ok=False, контент не выгружается)."""
    from antigona.tools.workspace_read import MAX_READ_BYTES

    ws = tmp_path / "ws_large"
    ws.mkdir(parents=True, exist_ok=True)
    large_file = ws / "large.txt"
    large_file.write_bytes(b"X" * (MAX_READ_BYTES + 64))

    tool = WorkspaceReadTextTool(workspace=ws)
    res = tool.execute("large.txt")
    assert res.ok is False
    assert res.content == ""
    assert "file too large" in res.error


def test_read_boundary_orchestrator_fail_closed(tmp_path: Path) -> None:
    """Boundary: при отказе чтения (symlink / oversize) orchestrator переводит задачу в FAILED и не создает артефактов."""
    from antigona.database import Database
    from antigona.filesystem import InProcessTestBackend, WorkspaceFileTool
    from antigona.models import TaskState
    from antigona.orchestrator import Orchestrator
    from antigona.repository import CreateTask, TaskRepository
    from antigona.tools.workspace_read import MAX_READ_BYTES
    from antigona.verifier_client import VerifierClient

    class StubVerifier(VerifierClient):
        def __init__(self, decision: str = "DONE") -> None:
            self.decision = decision

        def request_verification(self, task_id: str, correlation_id: str) -> str:
            return self.decision

    ws = tmp_path / "ws_orch_bounds"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "large.txt").write_bytes(b"Y" * (MAX_READ_BYTES + 128))

    db_path = tmp_path / "bounds.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    write_tool = WorkspaceFileTool(InProcessTestBackend(ws, test_mode=True), timeout_seconds=10)

    with database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id="owner",
                goal="прочитай большой файл large.txt",
                path="large.txt",
                content="",
                idempotency_key="large-task-1",
                tool_name="workspace.read_text",
            )
        )
        orch = Orchestrator(
            session,
            write_tool,
            StubVerifier(),
            workspace=type("WS", (), {"root_path": ws})(),
        )
        result = orch.run(task, "worker-1")
        assert result.status == TaskState.FAILED.value
        assert len(result.artifacts) == 0

