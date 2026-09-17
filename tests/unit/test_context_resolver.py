"""Tests for ContextResolver, SteeringQueue, SteeringClassifier, ReplyMappingStore.

New modules in Phase 1:
  - antigona.tasks.context_resolver
  - antigona.tasks.steering
  - antigona.channels.telegram.context_adapter (lightweight, no aiogram dep)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tasks.context_resolver import (
    ContextResolver,
    IntentType,
    ReplyMappingStore,
    ResolvedContext,
)
from antigona.tasks.steering import (
    SteeringClassifier,
    SteeringQueue,
    SteeringSignal,
)

# =============================================================================
# SteeringClassifier
# =============================================================================


class TestSteeringClassifier:
    @pytest.fixture
    def clf(self) -> SteeringClassifier:
        return SteeringClassifier()

    def test_continue_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("да", "ок", "окей", "продолжай", "начинай", "поехали", "ладно", "го"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.CONTINUE, f"{phrase} → {cmd.signal}"
            assert cmd.confidence >= 0.9

    def test_stop_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("нет", "стоп", "хватит", "stop", "no", "nope", "не надо"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.STOP, f"{phrase} → {cmd.signal}"

    def test_pause_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("пауза", "pause", "приостанови", "подожди", "wait"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.PAUSE, f"{phrase} → {cmd.signal}"

    def test_resume_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("продолжи", "resume", "возобнови", "go on"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.RESUME, f"{phrase} → {cmd.signal}"

    def test_cancel_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("отмени", "отмена", "отменить", "cancel", "abort"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.CANCEL, f"{phrase} → {cmd.signal}"

    def test_retry_exact(self, clf: SteeringClassifier) -> None:
        for phrase in ("повтори", "ещё раз", "заново", "retry", "сначала"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.RETRY, f"{phrase} → {cmd.signal}"

    def test_status_patterns(self, clf: SteeringClassifier) -> None:
        for phrase in ("как там?", "статус", "на каком этапе?", "что сделано", "прогресс", "отчёт"):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.STATUS, f"{phrase} → {cmd.signal}"

    def test_modify_patterns(self, clf: SteeringClassifier) -> None:
        for phrase in (
            "не используй shell",
            "сделай вместо python js",
            "добавь тесты",
            "убери лишние файлы",
            "измени подход",
            "замени библиотеку",
            "исправь ошибку",
        ):
            cmd = clf.classify(phrase)
            assert cmd.signal == SteeringSignal.MODIFY, f"{phrase} → {cmd.signal}"

    def test_first_word_fallback(self, clf: SteeringClassifier) -> None:
        """Фразы с командным словом в начале."""
        cases = [
            ("отмени задачу", SteeringSignal.CANCEL),
            ("отменить все", SteeringSignal.CANCEL),
            ("повтори попытку", SteeringSignal.RETRY),
            ("повтори еще раз", SteeringSignal.RETRY),
            ("приостанови работу", SteeringSignal.PAUSE),
            ("пауза на минуту", SteeringSignal.PAUSE),
            ("возобнови выполнение", SteeringSignal.RESUME),
            ("продолжай работу", SteeringSignal.RESUME),
            ("стоп машина", SteeringSignal.STOP),
            ("cancel task", SteeringSignal.CANCEL),
        ]
        for text, expected in cases:
            cmd = clf.classify(text)
            assert cmd.signal == expected, f"{text} → {cmd.signal} (expected {expected})"

    def test_empty_text(self, clf: SteeringClassifier) -> None:
        cmd = clf.classify("")
        assert cmd.signal == SteeringSignal.UNKNOWN
        assert cmd.confidence == 0.0

    def test_unknown(self, clf: SteeringClassifier) -> None:
        cmd = clf.classify("какой-то непонятный длинный текст без команды")
        assert cmd.signal == SteeringSignal.UNKNOWN

    def test_static_helpers(self) -> None:
        assert SteeringClassifier.is_steering_signal("да")
        assert SteeringClassifier.is_steering_signal("не используй shell")
        assert not SteeringClassifier.is_steering_signal("создай новый файл readme.txt с описанием")
        assert SteeringClassifier.get_signal("стоп") == SteeringSignal.STOP
        assert SteeringClassifier.get_signal("продолжай") == SteeringSignal.CONTINUE


# =============================================================================
# SteeringQueue
# =============================================================================


class TestSteeringQueue:
    @pytest.fixture
    def queue(self) -> SteeringQueue:
        return SteeringQueue()

    async def test_push_and_poll(self, queue: SteeringQueue) -> None:
        cmd = await queue.push(chat_id=1, task_id="t1", message="не используй shell")
        assert cmd.signal == SteeringSignal.MODIFY

        polled = await queue.poll(1, "t1")
        assert polled is not None
        assert polled.signal == SteeringSignal.MODIFY
        assert polled.original_text == "не используй shell"

    async def test_poll_empty(self, queue: SteeringQueue) -> None:
        assert await queue.poll(1, "nonexistent") is None

    async def test_count(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "да")
        await queue.push(1, "t1", "добавь тесты")
        assert await queue.count(1, "t1") == 2

    async def test_poll_all(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "да")
        await queue.push(1, "t1", "стоп")
        all_cmds = await queue.poll_all(1, "t1")
        assert len(all_cmds) == 2
        assert all_cmds[0].signal == SteeringSignal.CONTINUE
        assert all_cmds[1].signal == SteeringSignal.STOP

    async def test_peek(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "статус")
        peeked = await queue.peek(1, "t1")
        assert peeked is not None
        assert peeked.signal == SteeringSignal.STATUS
        # Peek does not remove
        assert await queue.count(1, "t1") == 1

    async def test_has_pending(self, queue: SteeringQueue) -> None:
        assert not await queue.has_pending(1, "t1")
        await queue.push(1, "t1", "да")
        assert await queue.has_pending(1, "t1")

    async def test_clear(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "да")
        await queue.clear(1, "t1")
        assert await queue.count(1, "t1") == 0

    async def test_clear_chat(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "да")
        await queue.push(1, "t2", "нет")
        await queue.clear_chat(1)
        assert await queue.count(1, "t1") == 0
        assert await queue.count(1, "t2") == 0

    async def test_per_task_isolation(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "да")
        await queue.push(2, "t2", "нет")
        assert await queue.count(1, "t1") == 1
        assert await queue.count(2, "t2") == 1
        assert await queue.count(1, "t2") == 0

    async def test_stats(self, queue: SteeringQueue) -> None:
        await queue.push(1, "t1", "а")
        await queue.push(1, "t1", "б")
        await queue.push(1, "t2", "в")
        stats = queue.stats
        assert stats["total_queues"] == 2
        assert stats["total_messages"] == 3


# =============================================================================
# ReplyMappingStore
# =============================================================================


class TestReplyMappingStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> ReplyMappingStore:
        return ReplyMappingStore(path=str(tmp_path / "test_messages.json"))

    def test_store_and_resolve(self, store: ReplyMappingStore) -> None:
        store.store_binding(chat_id=123, telegram_message_id=456, task_id="abc123")
        result = store.resolve_message(123, 456)
        assert result is not None
        assert result["task_id"] == "abc123"
        assert result["message_type"] == "status"

    def test_store_with_step_and_type(self, store: ReplyMappingStore) -> None:
        store.store_binding(123, 789, "task456", step_id="step001", message_type="result")
        result = store.resolve_message(123, 789)
        assert result is not None
        assert result["step_id"] == "step001"
        assert result["message_type"] == "result"

    def test_resolve_not_found(self, store: ReplyMappingStore) -> None:
        assert store.resolve_message(999, 999) is None

    def test_remove_binding(self, store: ReplyMappingStore) -> None:
        store.store_binding(1, 100, "t1")
        store.remove_binding(1, 100)
        assert store.resolve_message(1, 100) is None

    def test_get_tasks_for_chat(self, store: ReplyMappingStore) -> None:
        store.store_binding(1, 101, "t1")
        store.store_binding(1, 102, "t2")
        tasks = store.get_tasks_for_chat(1)
        assert len(tasks) == 2
        assert "telegram_msg_101" in tasks
        assert "telegram_msg_102" in tasks

    def test_clear_chat(self, store: ReplyMappingStore) -> None:
        store.store_binding(1, 101, "t1")
        store.store_binding(2, 201, "t2")
        store.clear_chat(1)
        assert len(store.get_tasks_for_chat(1)) == 0
        assert len(store.get_tasks_for_chat(2)) == 1

    def test_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "persist_test.json"
        store1 = ReplyMappingStore(path=str(path))
        store1.store_binding(1, 100, "task_id_123", message_type="question")

        # Новый экземпляр — читает тот же файл
        store2 = ReplyMappingStore(path=str(path))
        result = store2.resolve_message(1, 100)
        assert result is not None
        assert result["task_id"] == "task_id_123"
        assert result["message_type"] == "question"

    def test_created_at_timestamp(self, store: ReplyMappingStore) -> None:
        store.store_binding(1, 100, "t1")
        result = store.resolve_message(1, 100)
        assert result is not None
        assert "created_at" in result
        assert "T" in result["created_at"]  # ISO format


# =============================================================================
# ContextResolver — unit (stateless helpers + classification)
# =============================================================================


class TestContextResolver:
    @pytest.fixture
    async def resolver(self) -> ContextResolver:
        from antigona.tasks.event_bus import EventBus
        from antigona.tasks.manager import TaskManager

        bus = EventBus.get_instance()
        tm = TaskManager(event_bus=bus)
        return ContextResolver(task_manager=tm)

    # ── Intent classification helpers ──

    def test_classify_task_intent_control(self) -> None:
        """Управляющие команды через SteeringClassifier."""
        from antigona.tasks.models import Task

        task = Task(conversation_id=1, owner_user_id=1, title="Test")

        cases = [
            ("отмени задачу", IntentType.CANCEL),
            ("отмена", IntentType.CANCEL),
            ("стоп", IntentType.CANCEL),
            ("пауза", IntentType.PAUSE),
            ("приостанови", IntentType.PAUSE),
            ("продолжи", IntentType.RESUME),
            ("возобнови", IntentType.RESUME),
            ("повтори", IntentType.RETRY),
            ("повтори попытку", IntentType.RETRY),
            ("заново", IntentType.RETRY),
        ]
        for text, expected in cases:
            result = ContextResolver._classify_task_intent(text, task)
            assert result == expected, f"{text} → {result} (expected {expected})"

    def test_classify_task_intent_confirm_status(self) -> None:
        from antigona.tasks.models import Task

        task = Task(conversation_id=1, owner_user_id=1, title="Test")

        # Confirm
        assert ContextResolver._classify_task_intent("да", task) == IntentType.CONFIRM
        assert ContextResolver._classify_task_intent("поехали", task) == IntentType.CONFIRM

        # Status
        assert ContextResolver._classify_task_intent("как там?", task) == IntentType.STATUS_QUERY
        assert ContextResolver._classify_task_intent("статус", task) == IntentType.STATUS_QUERY

        # Steering
        assert ContextResolver._classify_task_intent(
            "не используй shell", task
        ) == IntentType.STEER_EXISTING
        # ADD_REQUIREMENT prefix ('добавь', 'ещё', 'также') wins
        assert ContextResolver._classify_task_intent(
            "добавь тесты", task
        ) == IntentType.ADD_REQUIREMENT

    def test_classify_new_intent(self) -> None:
        assert ContextResolver._classify_new_intent("создай файл") == IntentType.NEW_TASK
        assert ContextResolver._classify_new_intent("привет") == IntentType.NEW_TASK
        assert ContextResolver._classify_new_intent("статус") == IntentType.STATUS_QUERY
        assert ContextResolver._classify_new_intent("отмени") == IntentType.CANCEL

    # ── ResolvedContext dataclass ──

    def test_resolved_context_defaults(self) -> None:
        ctx = ResolvedContext(
            intent=IntentType.NEW_TASK,
            task_id=None,
            conversation_id=1,
            user_message="hello",
            original_message_id=100,
            response_to_message_id=None,
        )
        assert ctx.revision == 0
        assert ctx.steered_text == ""
        assert ctx.confidence == 1.0
        assert ctx.metadata == {}

    def test_resolved_context_full(self) -> None:
        ctx = ResolvedContext(
            intent=IntentType.CORRECT_MESSAGE,
            task_id="abc",
            conversation_id=1,
            user_message="исправлено",
            original_message_id=100,
            response_to_message_id=99,
            revision=2,
            steered_text="исправлено v2",
            confidence=0.95,
            metadata={"source": "edit"},
        )
        assert ctx.revision == 2
        assert ctx.steered_text == "исправлено v2"
        assert ctx.metadata["source"] == "edit"


# =============================================================================
# ContextResolver — async integration (resolve pipeline)
# =============================================================================


@pytest.mark.asyncio
class TestContextResolverAsync:
    @pytest.fixture(autouse=True)
    def _reset_bus(self) -> None:
        from antigona.tasks.event_bus import EventBus

        EventBus.reset_instance()

    @pytest.fixture
    async def tm_and_resolver(self, tmp_path: Path) -> tuple:
        from antigona.tasks.event_bus import EventBus
        from antigona.tasks.manager import TaskManager

        bus = EventBus.get_instance(
            persist_path=str(tmp_path / "events.jsonl")
        )
        tm = TaskManager(
            event_bus=bus,
            tasks_persist_path=str(tmp_path / "tasks.json"),
        )
        await tm.start()
        resolver = ContextResolver(task_manager=tm, reply_store=None)
        return tm, resolver

    async def test_resolve_new_task(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        ctx = await resolver.resolve(
            text="создай новый проект",
            chat_id=100, user_id=1,
            message_id=1,
        )
        assert ctx.intent == IntentType.NEW_TASK
        assert ctx.task_id is None
        assert ctx.conversation_id == 100
        assert ctx.confidence > 0
        # steered_text for new task = original text
        assert ctx.steered_text == "создай новый проект"

    async def test_resolve_reply_known_message(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=100, owner_user_id=1,
            title="Test", original_request="do it",
        )
        # Bind message in ReplyMappingStore
        resolver.reply_store.store_binding(100, 500, task.task_id, "status")

        ctx = await resolver.resolve(
            text="не используй shell",
            chat_id=100, user_id=1,
            message_id=501, reply_to_message_id=500,
        )
        assert ctx.task_id == task.task_id
        assert ctx.intent == IntentType.STEER_EXISTING
        assert ctx.metadata.get("source") == "reply_new"
        assert ctx.response_to_message_id == 500

    async def test_resolve_reply_steer(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=100, owner_user_id=1,
            title="Steer", original_request="сделай сайт",
        )
        resolver.reply_store.store_binding(100, 500, task.task_id, "status")

        # Cancel via reply
        ctx = await resolver.resolve(
            text="отмени задачу",
            chat_id=100, user_id=1,
            message_id=502, reply_to_message_id=500,
        )
        assert ctx.intent == IntentType.CANCEL
        assert ctx.task_id == task.task_id

        # Status via reply
        ctx2 = await resolver.resolve(
            text="как там?",
            chat_id=100, user_id=1,
            message_id=503, reply_to_message_id=500,
        )
        assert ctx2.intent == IntentType.STATUS_QUERY
        assert ctx2.task_id == task.task_id

    async def test_resolve_edit(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=100, owner_user_id=1,
            title="Edit test", original_request="сделай А",
        )
        resolver.reply_store.store_binding(100, 600, task.task_id, "status")

        ctx = await resolver.resolve(
            text="сделай Б вместо А",
            chat_id=100, user_id=1,
            message_id=600, edited_message_id=600,
        )
        assert ctx.intent == IntentType.CORRECT_MESSAGE
        assert ctx.task_id == task.task_id
        assert ctx.revision == 1

    async def test_resolve_single_active(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=200, owner_user_id=1,
            title="Active", original_request="напиши код",
        )

        # No reply, no edit, no explicit ID → single active task match
        ctx = await resolver.resolve(
            text="продолжай",
            chat_id=200, user_id=1,
            message_id=700,
        )
        assert ctx.task_id == task.task_id
        assert ctx.intent == IntentType.CONFIRM
        assert ctx.metadata.get("source") == "single_active"

    async def test_resolve_explicit_id(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=300, owner_user_id=1,
            title="ID test", original_request="сделай",
        )

        ctx = await resolver.resolve(
            text=f"статус {task.task_id}",
            chat_id=300, user_id=1,
            message_id=800,
        )
        assert ctx.task_id == task.task_id
        assert ctx.intent == IntentType.STATUS_QUERY
        assert ctx.metadata.get("source") == "explicit_id"

    async def test_resolve_semantic_match(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        await tm.create_task(
            conversation_id=400, owner_user_id=1,
            title="Semantic", original_request="напиши скрипт для бэкапа баз данных",
            current_request="напиши скрипт для бэкапа баз данных",
        )

        ctx = await resolver.resolve(
            text="добавь в скрипт логирование",
            chat_id=400, user_id=1,
            message_id=900,
        )
        # Should match by semantic similarity (text match); SteeringClassifier
        # runs first, so a leading "добавь" classifies as ADD_REQUIREMENT.
        assert ctx.task_id is not None
        assert ctx.intent == IntentType.ADD_REQUIREMENT
        assert ctx.metadata.get("source") in ("semantic_match", "single_active")

    async def test_resolve_edit_increments_revision(self, tm_and_resolver: tuple) -> None:
        tm, resolver = tm_and_resolver
        task = await tm.create_task(
            conversation_id=100, owner_user_id=1,
            title="Revision", original_request="версия 1",
        )
        resolver.reply_store.store_binding(100, 1000, task.task_id, "status")

        # Первый edit
        ctx1 = await resolver.resolve(
            text="версия 2",
            chat_id=100, user_id=1,
            message_id=1000, edited_message_id=1000,
        )
        assert ctx1.revision == 1

        # Второй edit
        ctx2 = await resolver.resolve(
            text="версия 3",
            chat_id=100, user_id=1,
            message_id=1000, edited_message_id=1000,
        )
        assert ctx2.revision == 2
