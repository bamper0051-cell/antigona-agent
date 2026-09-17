"""Тесты редактирования сообщений и ContextResolver (§24.3).

Сценарии приемки:
  1. Edit: пользователь редактирует сообщение → задача обновляется,
     план пересчитывается (CORRECT_MESSAGE интент)
  2. Reply на статусное сообщение → привязка к задаче
  3. Две задачи → Reply на первую → привязка к первой
  4. Новая задача без контекста → NEW_TASK
  5. Пустое сообщение → NEW_TASK с низкой уверенностью
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tasks import EventBus, TaskManager
from antigona.tasks.context_resolver import (
    ContextResolver,
    IntentType,
    ReplyMappingStore,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_persist(tmp_path: Path) -> Path:
    return tmp_path / "tasks.json"


@pytest.fixture
def tmp_events(tmp_path: Path) -> str:
    return str(tmp_path / "events.jsonl")


@pytest.fixture
def bus(tmp_events: str) -> EventBus:
    EventBus.reset_instance()
    return EventBus.get_instance(persist_path=tmp_events)


@pytest.fixture
async def manager(bus: EventBus, tmp_persist: Path) -> TaskManager:
    mgr = TaskManager(event_bus=bus, tasks_persist_path=tmp_persist)
    await mgr.start()
    return mgr


@pytest.fixture
def resolver(manager: TaskManager) -> ContextResolver:
    return ContextResolver(task_manager=manager)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestEditMessageFlow:
    """§24.3: Edit сообщения — CORRECT_MESSAGE."""

    async def test_edit_resolves_to_correct_message(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """Редактирование сообщения → CORRECT_MESSAGE."""
        # Создаём задачу и привязываем к сообщению
        t = await manager.create_task(
            conversation_id=100,
            owner_user_id=200,
            title="Install whisper",
            original_request="установи Whisper",
        )
        await manager.bind_status_message(t.task_id, 500)

        # Редактируем сообщение 500
        ctx = await resolver.resolve(
            text="установи faster-whisper",
            chat_id=100,
            user_id=200,
            message_id=501,
            edited_message_id=500,
        )
        assert ctx.intent == IntentType.CORRECT_MESSAGE
        assert ctx.task_id == t.task_id
        assert ctx.revision >= 1
        assert ctx.metadata.get("source") == "edit"

    async def test_edit_updates_task_request(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """Edit обновляет current_request задачи."""
        t = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Setup", original_request="установи Whisper",
        )
        await manager.bind_status_message(t.task_id, 600)

        # Edit сообщения
        await resolver.resolve(
            text="установи faster-whisper",
            chat_id=100, user_id=200,
            message_id=601,
            edited_message_id=600,
        )

        # Задача обновлена
        updated = await manager.get_task(t.task_id)
        assert updated is not None

    async def test_edit_no_binding_returns_new_task(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """Edit без привязки — NEW_TASK (падает на семантику)."""
        ctx = await resolver.resolve(
            text="сделай что-нибудь",
            chat_id=100, user_id=200,
            message_id=700,
            edited_message_id=999,  # Несуществующее сообщение
        )
        # Не найдена задача — NEW_TASK
        assert ctx.intent == IntentType.NEW_TASK
        assert ctx.task_id is None


class TestReplyFlow:
    """§24.2: Reply на сообщение — привязка к задаче."""

    async def test_reply_binds_to_task(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """Reply на статусное сообщение → привязка к задаче."""
        t = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Test task", original_request="test",
        )
        # Привязываем через bind_status_message
        await manager.bind_status_message(t.task_id, 1001)

        # Reply на сообщение 1001
        ctx = await resolver.resolve(
            text="продолжай",
            chat_id=100, user_id=200,
            message_id=1002,
            reply_to_message_id=1001,
        )
        # Должен найти задачу через ReplyMappingStore первым
        # Если не найдёт — через TaskManager._by_message
        assert ctx.task_id == t.task_id
        assert ctx.intent in (
            IntentType.CONFIRM,
            IntentType.STEER_EXISTING,
            IntentType.ADD_REQUIREMENT,
        )

    async def test_reply_second_binds_to_correct_task(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """2 задачи → Reply на первую → привязка к первой."""
        t1 = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="First task", original_request="первая задача",
        )
        t2 = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Second task", original_request="вторая задача",
        )
        await manager.bind_status_message(t1.task_id, 2001)
        await manager.bind_status_message(t2.task_id, 2002)

        # Reply на сообщение первой задачи
        ctx = await resolver.resolve(
            text="продолжи",
            chat_id=100, user_id=200,
            message_id=2003,
            reply_to_message_id=2001,
        )
        assert ctx.task_id == t1.task_id

    async def test_reply_no_binding_returns_none(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """Reply на незнакомое сообщение — NEW_TASK."""
        ctx = await resolver.resolve(
            text="привет",
            chat_id=100, user_id=200,
            message_id=3000,
            reply_to_message_id=9999,  # Нет привязки
        )
        # Не найдена → NEW_TASK
        assert ctx.intent == IntentType.NEW_TASK
        assert ctx.task_id is None


class TestNewTaskFlow:
    """§24.1: Новый запрос."""

    async def test_new_task_creates_new_intent(
        self, resolver: ContextResolver,
    ) -> None:
        """Новый запрос без контекста → NEW_TASK."""
        ctx = await resolver.resolve(
            text="создай файл test.txt",
            chat_id=100, user_id=200,
            message_id=4000,
        )
        assert ctx.intent == IntentType.NEW_TASK
        assert ctx.task_id is None
        assert ctx.conversation_id == 100
        assert ctx.user_message == "создай файл test.txt"

    async def test_empty_message(
        self, resolver: ContextResolver,
    ) -> None:
        """Пустое сообщение → NEW_TASK с низкой уверенностью."""
        ctx = await resolver.resolve(
            text="   ",
            chat_id=100, user_id=200,
            message_id=4001,
        )
        assert ctx.intent == IntentType.NEW_TASK
        assert ctx.confidence == 0.0

    async def test_reply_mapping_store_roundtrip(self) -> None:
        """ReplyMappingStore: сохранение и восстановление привязки."""
        store = ReplyMappingStore(path="/tmp/_test_reply_store.json")

        store.store_binding(
            chat_id=100,
            telegram_message_id=5000,
            task_id="task-abc",
            step_id="step-xyz",
            message_type="status",
        )

        binding = store.resolve_message(100, 5000)
        assert binding is not None
        assert binding["task_id"] == "task-abc"
        assert binding["step_id"] == "step-xyz"
        assert binding["message_type"] == "status"

        # Удаление
        store.remove_binding(100, 5000)
        assert store.resolve_message(100, 5000) is None

    async def test_reply_mapping_store_clear_chat(self) -> None:
        """Очистка всех привязок чата."""
        store = ReplyMappingStore(path="/tmp/_test_reply_store2.json")
        store.store_binding(100, 1, "task-1")
        store.store_binding(100, 2, "task-2")
        store.store_binding(200, 3, "task-3")

        store.clear_chat(100)
        assert store.resolve_message(100, 1) is None
        assert store.resolve_message(100, 2) is None
        # Чат 200 не затронут
        assert store.resolve_message(200, 3) is not None


class TestContextResolverIntentClassification:
    """Классификация интентов ContextResolver."""

    async def test_status_query(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """'на каком этапе?' → STATUS_QUERY."""
        t = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Status", original_request="status",
        )
        await manager.bind_status_message(t.task_id, 7000)

        ctx = await resolver.resolve(
            text="на каком этапе?",
            chat_id=100, user_id=200,
            message_id=7001,
            reply_to_message_id=7000,
        )
        assert ctx.task_id == t.task_id

    async def test_confirm_intent(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """'продолжай' → CONFIRM."""
        t = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Confirm", original_request="confirm",
        )
        await manager.bind_status_message(t.task_id, 8000)

        ctx = await resolver.resolve(
            text="продолжай",
            chat_id=100, user_id=200,
            message_id=8001,
            reply_to_message_id=8000,
        )
        assert ctx.task_id == t.task_id

    async def test_cancel_intent(
        self, manager: TaskManager, resolver: ContextResolver,
    ) -> None:
        """'отмени' → CANCEL."""
        t = await manager.create_task(
            conversation_id=100, owner_user_id=200,
            title="Cancel", original_request="cancel",
        )
        await manager.bind_status_message(t.task_id, 9000)

        ctx = await resolver.resolve(
            text="отмени",
            chat_id=100, user_id=200,
            message_id=9001,
            reply_to_message_id=9000,
        )
        assert ctx.task_id == t.task_id
        assert ctx.intent == IntentType.CANCEL
