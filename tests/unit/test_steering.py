"""Тесты Steering — классификация сигналов и очередь управления (§24.4).

Сценарии приемки:
  1. Steering-сигналы классифицируются (CONTINUE, STOP, MODIFY, PAUSE, RESUME, CANCEL, STATUS)
  2. Очередь steering-сигналов
  3. Задача выполняется → 'не используй глобальный Python' → обновляет задачу
"""

from __future__ import annotations

import pytest

from antigona.tasks import EventBus, TaskManager
from antigona.tasks.steering import (
    SteeringClassifier,
    SteeringQueue,
    SteeringSignal,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def queue() -> SteeringQueue:
    return SteeringQueue()


@pytest.fixture
def classifier() -> SteeringClassifier:
    return SteeringClassifier()


# ── Tests: SteeringClassifier ────────────────────────────────────────────────


class TestSteeringClassifier:
    """§8, §24.4: Классификация steering-сигналов."""

    def test_continue_exact(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("да")
        assert cmd.signal == SteeringSignal.CONTINUE
        assert cmd.confidence >= 0.9

    def test_continue_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["ок", "продолжай", "yes", "ok", "давай", "го", "поехали"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.CONTINUE, f"'{text}' → {cmd.signal}"

    def test_stop(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("стоп")
        assert cmd.signal == SteeringSignal.STOP

    def test_stop_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["нет", "не надо", "хватит", "stop", "halt"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.STOP, f"'{text}' → {cmd.signal}"

    def test_pause(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("пауза")
        assert cmd.signal == SteeringSignal.PAUSE

    def test_resume(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("продолжи")
        assert cmd.signal == SteeringSignal.RESUME

    def test_cancel(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("отмени")
        assert cmd.signal == SteeringSignal.CANCEL

    def test_cancel_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["отмена", "cancel", "abort"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.CANCEL, f"'{text}' → {cmd.signal}"

    def test_retry(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("повтори")
        assert cmd.signal == SteeringSignal.RETRY

    def test_retry_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["ещё раз", "заново", "retry"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.RETRY, f"'{text}' → {cmd.signal}"

    def test_status_query(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("как там?")
        assert cmd.signal == SteeringSignal.STATUS

    def test_status_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["на каком этапе?", "статус", "что сделано?", "прогресс"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.STATUS, f"'{text}' → {cmd.signal}"

    def test_modify_dont_use(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("не используй глобальный Python")
        assert cmd.signal == SteeringSignal.MODIFY
        assert "глобальный" in cmd.modification_text

    def test_modify_variants(self, classifier: SteeringClassifier) -> None:
        for text in ["сделай вместо этого", "добавь проверку", "измени подход", "используй другой пакет"]:
            cmd = classifier.classify(text)
            assert cmd.signal == SteeringSignal.MODIFY, f"'{text}' → {cmd.signal}"

    def test_unknown(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("совершенно случайный текст")
        assert cmd.signal in (SteeringSignal.MODIFY, SteeringSignal.UNKNOWN)

    def test_empty_text(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("")
        assert cmd.signal == SteeringSignal.UNKNOWN
        assert cmd.confidence == 0.0

    def test_whitespace_text(self, classifier: SteeringClassifier) -> None:
        cmd = classifier.classify("   ")
        assert cmd.signal == SteeringSignal.UNKNOWN

    def test_first_word_cancel(self, classifier: SteeringClassifier) -> None:
        """Фраза 'отмени задачу' — первое слово определяет CANCEL."""
        cmd = classifier.classify("отмени задачу")
        assert cmd.signal == SteeringSignal.CANCEL

    def test_first_word_retry(self, classifier: SteeringClassifier) -> None:
        """Фраза 'повтори попытку' — первое слово определяет RETRY."""
        cmd = classifier.classify("повтори попытку")
        assert cmd.signal == SteeringSignal.RETRY

    def test_is_steering_signal(self) -> None:
        assert SteeringClassifier.is_steering_signal("да") is True
        assert SteeringClassifier.is_steering_signal("стоп") is True
        # "установи Python" — короткое сообщение (2 слова), классифицируется как MODIFY
        assert SteeringClassifier.is_steering_signal("установи Python") is True
        # Длинное нейтральное сообщение без ключевых слов — UNKNOWN
        assert SteeringClassifier.is_steering_signal("сегодня хорошая погода для прогулки в парке") is False

    def test_get_signal(self) -> None:
        assert SteeringClassifier.get_signal("да") == SteeringSignal.CONTINUE
        assert SteeringClassifier.get_signal("отмени") == SteeringSignal.CANCEL


# ── Tests: SteeringQueue ────────────────────────────────────────────────────


class TestSteeringQueue:
    """§8: Очередь steering-сигналов."""

    async def test_push_and_poll(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="стоп")
        cmd = await queue.poll(100, "t1")
        assert cmd is not None
        assert cmd.signal == SteeringSignal.STOP

    async def test_poll_empty(self, queue: SteeringQueue) -> None:
        cmd = await queue.poll(100, "t1")
        assert cmd is None

    async def test_poll_all(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="стоп")
        await queue.push(chat_id=100, task_id="t1", message="да")
        cmds = await queue.poll_all(100, "t1")
        assert len(cmds) == 2
        assert cmds[0].signal == SteeringSignal.STOP
        assert cmds[1].signal == SteeringSignal.CONTINUE
        # Очередь пуста
        assert await queue.poll(100, "t1") is None

    async def test_peek(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="пауза")
        cmd = await queue.peek(100, "t1")
        assert cmd is not None
        assert cmd.signal == SteeringSignal.PAUSE
        # Сообщение осталось в очереди
        assert await queue.count(100, "t1") == 1

    async def test_peek_empty(self, queue: SteeringQueue) -> None:
        assert await queue.peek(100, "t1") is None

    async def test_count(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="1")
        await queue.push(chat_id=100, task_id="t1", message="2")
        assert await queue.count(100, "t1") == 2

    async def test_clear(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="стоп")
        await queue.push(chat_id=100, task_id="t1", message="cancel")
        await queue.clear(100, "t1")
        assert await queue.count(100, "t1") == 0

    async def test_clear_chat(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="1")
        await queue.push(chat_id=100, task_id="t2", message="2")
        await queue.clear_chat(100)
        assert await queue.count(100, "t1") == 0
        assert await queue.count(100, "t2") == 0

    async def test_has_pending(self, queue: SteeringQueue) -> None:
        assert await queue.has_pending(100, "t1") is False
        await queue.push(chat_id=100, task_id="t1", message="да")
        assert await queue.has_pending(100, "t1") is True

    async def test_stats(self, queue: SteeringQueue) -> None:
        await queue.push(chat_id=100, task_id="t1", message="1")
        await queue.push(chat_id=100, task_id="t1", message="2")
        await queue.push(chat_id=200, task_id="t2", message="3")
        stats = queue.stats
        assert stats["total_queues"] == 2
        assert stats["total_messages"] == 3


# ── Integration: steering flow ──────────────────────────────────────────────


class TestSteeringTaskIntegration:
    """§24.4: Steering интеграция с TaskManager."""

    async def test_steer_updates_task_request(self) -> None:
        """Steering-сигнал обновляет current_request задачи."""
        bus = EventBus.get_instance()
        mgr = TaskManager(event_bus=bus)
        await mgr.start()

        t = await mgr.create_task(
            conversation_id=100, owner_user_id=200,
            title="Install Python", original_request="установи Python 3.11",
        )
        # Steering через TaskManager
        await mgr.steer_task(t.task_id, "установи Python 3.12, не используй глобальный")
        updated = await mgr.get_task(t.task_id)
        assert updated is not None
        assert "Python 3.12" in updated.current_request

        # Проверяем событие
        events = bus.get_events(t.task_id, event_type="TASK_STEERED")
        assert len(events) == 1
        assert events[0]["payload"]["new_request"] == "установи Python 3.12, не используй глобальный"

    async def test_steering_queue_with_task_manager(self) -> None:
        """SteeringQueue + TaskManager: сигнал доставляется."""
        bus = EventBus.get_instance()
        mgr = TaskManager(event_bus=bus)
        await mgr.start()

        t = await mgr.create_task(
            conversation_id=100, owner_user_id=200,
            title="Test", original_request="create file",
        )

        q = SteeringQueue()
        await q.push(chat_id=100, task_id=t.task_id, message="не используй shell")

        cmd = await q.poll(100, t.task_id)
        assert cmd is not None
        assert cmd.signal == SteeringSignal.MODIFY
        assert "shell" in cmd.modification_text
