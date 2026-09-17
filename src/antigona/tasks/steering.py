"""Steering — очередь команд управления задачами.

Позволяет направлять steering-сообщения в активные задачи без прерывания
их выполнения. Сообщения накапливаются в очереди и проверяются TaskRunner
перед каждым новым шагом.

Компоненты:
  SteeringSignal — enum типов сигналов управления
  SteeringCommand — dataclass с распарсенной командой
  SteeringQueue  — очередь steering-сообщений для задач
  SteeringClassifier — классификация коротких сообщений как сигналов
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ── SteeringSignal ─────────────────────────────────────────────────────────


class SteeringSignal(StrEnum):
    """Тип steering-сигнала."""

    CONTINUE = "CONTINUE"
    """Продолжить выполнение (подтверждение)."""

    STOP = "STOP"
    """Остановить выполнение (немедленно, без отмены задачи)."""

    MODIFY = "MODIFY"
    """Изменить подход/инструменты/поведение."""

    PAUSE = "PAUSE"
    """Приостановить задачу."""

    RESUME = "RESUME"
    """Возобновить приостановленную задачу."""

    CANCEL = "CANCEL"
    """Отменить задачу полностью."""

    RETRY = "RETRY"
    """Повторить последний шаг или всю задачу."""

    STATUS = "STATUS"
    """Запрос статуса."""

    UNKNOWN = "UNKNOWN"
    """Не удалось классифицировать."""


# ── SteeringCommand ────────────────────────────────────────────────────────


@dataclass
class SteeringCommand:
    """Распарсенная команда управления задачей.

    Attributes:
        signal: Тип сигнала.
        original_text: Исходный текст сообщения.
        modification_text: Текст модификации (для MODIFY-сигналов).
        confidence: Уверенность классификации 0.0–1.0.
        timestamp: ISO-метка получения.
    """

    signal: SteeringSignal = SteeringSignal.UNKNOWN
    original_text: str = ""
    modification_text: str = ""
    confidence: float = 0.0
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


# ── SteeringQueue ──────────────────────────────────────────────────────────


class SteeringQueue:
    """Очередь steering-команд для активных задач.

    Каждая задача может получать steering-сообщения во время выполнения.
    Новые сообщения НЕ прерывают задачу — они накапливаются в очереди.
    TaskRunner проверяет очередь перед каждым новым шагом.

    Потокобезопасность: asyncio.Lock для конкурентного доступа.

    Usage::

        queue = SteeringQueue()
        queue.push(chat_id=123, task_id="abc", message="не используй shell")

        cmd = queue.poll(chat_id=123, task_id="abc")
        if cmd is not None:
            if cmd.signal == SteeringSignal.MODIFY:
                print(f"Модификация: {cmd.modification_text}")
    """

    def __init__(self) -> None:
        import asyncio

        # (chat_id, task_id) -> list[SteeringCommand]
        self._queues: dict[tuple[int, str], list[SteeringCommand]] = {}
        self._lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    async def push(
        self,
        chat_id: int,
        task_id: str,
        message: str,
    ) -> SteeringCommand:
        """Добавить steering-команду в очередь задачи.

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.
            message: Текст сообщения от пользователя.

        Returns:
            Распознанный SteeringCommand.
        """
        classifier = SteeringClassifier()
        cmd = classifier.classify(message)

        async with self._lock:
            key = (chat_id, task_id)
            self._queues.setdefault(key, []).append(cmd)
            logger.debug(
                "SteeringQueue: [%s] добавлен сигнал %s для задачи %s (conv=%d)",
                message[:50],
                cmd.signal.value,
                task_id[:8],
                chat_id,
            )

        return cmd

    async def poll(
        self,
        chat_id: int,
        task_id: str,
    ) -> SteeringCommand | None:
        """Забрать первый steering-сигнал из очереди (non-blocking).

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.

        Returns:
            SteeringCommand или None (очередь пуста).
        """
        async with self._lock:
            key = (chat_id, task_id)
            queue = self._queues.get(key)
            if not queue:
                return None
            cmd = queue.pop(0)
            # Очищаем пустой список, чтобы не плодить мусор
            if not queue:
                del self._queues[key]
            return cmd

    async def poll_all(
        self,
        chat_id: int,
        task_id: str,
    ) -> list[SteeringCommand]:
        """Забрать ВСЕ steering-сигналы из очереди сразу.

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.

        Returns:
            Список SteeringCommand (может быть пустым).
        """
        async with self._lock:
            key = (chat_id, task_id)
            queue = self._queues.pop(key, [])
            return queue

    async def peek(
        self,
        chat_id: int,
        task_id: str,
    ) -> SteeringCommand | None:
        """Посмотреть первый сигнал без извлечения.

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.

        Returns:
            SteeringCommand или None.
        """
        async with self._lock:
            key = (chat_id, task_id)
            queue = self._queues.get(key)
            if not queue:
                return None
            return queue[0]

    async def count(
        self,
        chat_id: int,
        task_id: str,
    ) -> int:
        """Количество ожидающих сигналов для задачи.

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.

        Returns:
            Число сигналов в очереди.
        """
        async with self._lock:
            key = (chat_id, task_id)
            queue = self._queues.get(key)
            return len(queue) if queue else 0

    async def clear(
        self,
        chat_id: int,
        task_id: str,
    ) -> None:
        """Очистить очередь для задачи (без обработки).

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.
        """
        async with self._lock:
            key = (chat_id, task_id)
            self._queues.pop(key, None)

    async def clear_chat(self, chat_id: int) -> None:
        """Очистить все очереди для чата.

        Args:
            chat_id: Telegram chat ID.
        """
        async with self._lock:
            keys_to_delete = [k for k in self._queues if k[0] == chat_id]
            for k in keys_to_delete:
                del self._queues[k]

    async def has_pending(self, chat_id: int, task_id: str) -> bool:
        """Проверить, есть ли ожидающие сигналы для задачи.

        Args:
            chat_id: Telegram chat ID.
            task_id: UUID задачи.

        Returns:
            True если есть хотя бы один сигнал.
        """
        return await self.count(chat_id, task_id) > 0

    @property
    def stats(self) -> dict[str, Any]:
        """Статистика очереди."""
        return {
            "total_queues": len(self._queues),
            "total_messages": sum(len(q) for q in self._queues.values()),
        }


# ── SteeringClassifier ─────────────────────────────────────────────────────


class SteeringClassifier:
    """Классификация коротких сообщений как steering-сигналов.

    Анализирует текст на русском и английском языке,
    определяет тип сигнала управления.

    Правила классификации (по спецификации §8):

    - "да", "ок", "продолжай", "начинай", "поехали" → CONTINUE
    - "нет", "не надо", "стоп", "хватит" → STOP
    - "не используй...", "сделай вместо...", "добавь..." → MODIFY
    - "приостанови", "пауза" → PAUSE
    - "возобнови", "продолжи" → RESUME
    - "отмени", "отмена" → CANCEL
    - "повтори", "ещё раз", "заново" → RETRY
    - "как там?", "статус", "на каком этапе?" → STATUS
    """

    # ── Паттерны для классификации ────────────────────────────────────────

    # Точные совпадения (короткие команды)
    _EXACT_CONTINUE = frozenset({
        "да", "ок", "окей", "ok", "yes", "ага", "начинай", "поехали",
        "y", "д", "+", "ладно", "хорошо", "го", "давай",
        "continue", "go", "start", "do it", "go ahead", "yeah", "yep",
        "валяй", "продолжай", "конечно",
    })

    _EXACT_STOP = frozenset({
        "нет", "не надо", "стоп", "хватит", "stop", "no", "nope",
        "прекрати", "halt", "enough",
    })

    _EXACT_PAUSE = frozenset({
        "пауза", "pause", "приостанови", "приостановить",
        "подожди", "wait", "hold on", "hold",
    })

    _EXACT_RESUME = frozenset({
        "продолжи", "продолжай", "resume", "возобнови", "возобновить",
        "продолжаем", "go on",
    })

    _EXACT_CANCEL = frozenset({
        "отмени", "отмена", "отменить", "cancel", "отменяю",
        "abort", "отбой",
    })

    _EXACT_RETRY = frozenset({
        "повтори", "ещё раз", "заново", "retry", "попробуй снова",
        "попробуй ещё", "повтор", "сначала", "повторно",
    })

    # Паттерны для статуса (содержат ключевые слова)
    _STATUS_PATTERNS = (
        r"\bкак\s+там\b",
        r"\bна\s+каком\s+этап",
        r"\bчто\s+сделано\b",
        r"\bкакой\s+статус\b",
        r"\bстатус\b",
        r"\bпрогресс\b",
        r"\bprogress\b",
        r"\bчто\s+готово\b",
        r"\bсколько\s+осталось\b",
        r"\bчё\s+там\b",
        r"\bчё\s+по\b",
        r"\bкак\s+успехи\b",
        r"\bгде\s+мы\b",
        r"\bстатус\s+задачи\b",
        r"\bотчёт\b",
        r"\breport\b",
    )

    # Паттерны для модификации (содержат инструкции изменения)
    _MODIFY_PATTERNS = (
        r"\bне\s+используй\b",
        r"\bсделай\s+вместо\b",
        r"\bдобавь\b",
        r"\bубери\b",
        r"\bизмени\b",
        r"\bзамени\b",
        r"\bвместо\b",
        r"\bиспользуй\s+другой\b",
        r"\bпопробуй\s+по-другому\b",
        r"\bсделай\s+иначе\b",
        r"\bне\s+надо\b",
        r"\bне\s+нужно\b",
        r"\bисправь\b",
        r"\bобнови\b",
        r"\bпеределай\b",
        r"\bпоменяй\b",
    )

    def __init__(self) -> None:
        self._status_re = re.compile(
            "|".join(self._STATUS_PATTERNS), re.IGNORECASE
        )
        self._modify_re = re.compile(
            "|".join(self._MODIFY_PATTERNS), re.IGNORECASE
        )
        # Строим карту первое-слово → сигнал для фраз вида "отмени задачу"
        self._first_word_map: dict[str, SteeringSignal] = {}
        for exact_set, signal in [
            (self._EXACT_CONTINUE, SteeringSignal.CONTINUE),
            (self._EXACT_STOP, SteeringSignal.STOP),
            (self._EXACT_PAUSE, SteeringSignal.PAUSE),
            (self._EXACT_RESUME, SteeringSignal.RESUME),
            (self._EXACT_CANCEL, SteeringSignal.CANCEL),
            (self._EXACT_RETRY, SteeringSignal.RETRY),
        ]:
            for word in exact_set:
                # Только однословные команды (без пробелов)
                if " " not in word:
                    self._first_word_map[word] = signal

    # ── Основной метод ────────────────────────────────────────────────────

    def classify(self, text: str) -> SteeringCommand:
        """Классифицировать текст как steering-сигнал.

        Args:
            text: Текст сообщения пользователя.

        Returns:
            SteeringCommand с определённым сигналом.
        """
        lower = text.lower().strip()
        if not lower:
            return SteeringCommand(
                signal=SteeringSignal.UNKNOWN,
                original_text=text,
                confidence=0.0,
            )

        # ── Шаг 1: Точные совпадения (короткие команды) ──────────────────
        if lower in self._EXACT_CONTINUE:
            return SteeringCommand(
                signal=SteeringSignal.CONTINUE,
                original_text=text,
                confidence=0.95,
            )

        if lower in self._EXACT_STOP:
            return SteeringCommand(
                signal=SteeringSignal.STOP,
                original_text=text,
                confidence=0.95,
            )

        if lower in self._EXACT_PAUSE:
            return SteeringCommand(
                signal=SteeringSignal.PAUSE,
                original_text=text,
                confidence=0.95,
            )

        if lower in self._EXACT_RESUME:
            return SteeringCommand(
                signal=SteeringSignal.RESUME,
                original_text=text,
                confidence=0.95,
            )

        if lower in self._EXACT_CANCEL:
            return SteeringCommand(
                signal=SteeringSignal.CANCEL,
                original_text=text,
                confidence=0.95,
            )

        if lower in self._EXACT_RETRY:
            return SteeringCommand(
                signal=SteeringSignal.RETRY,
                original_text=text,
                confidence=0.95,
            )

        # ── Шаг 2: Паттерны (более длинные сообщения) ────────────────────

        # 2a. Проверка первого слова (фразы "отмени задачу", "повтори попытку")
        first_word = lower.split()[0] if lower.split() else ""
        if first_word in self._first_word_map:
            signal = self._first_word_map[first_word]
            return SteeringCommand(
                signal=signal,
                original_text=text,
                confidence=0.9,
            )

        # Модификация (приоритет выше статуса, т.к. м.б. "не используй...")
        modify_match = self._modify_re.search(lower)
        if modify_match:
            return SteeringCommand(
                signal=SteeringSignal.MODIFY,
                original_text=text,
                modification_text=text,
                confidence=0.85,
            )

        # Статус
        status_match = self._status_re.search(lower)
        if status_match:
            return SteeringCommand(
                signal=SteeringSignal.STATUS,
                original_text=text,
                confidence=0.9,
            )

        # ── Шаг 3: Не распознано ─────────────────────────────────────────

        # Если сообщение очень короткое (1-3 слова) — предполагаем MODIFY
        word_count = len(lower.split())
        if word_count <= 3:
            return SteeringCommand(
                signal=SteeringSignal.MODIFY,
                original_text=text,
                modification_text=text,
                confidence=0.5,
            )

        return SteeringCommand(
            signal=SteeringSignal.UNKNOWN,
            original_text=text,
            confidence=0.3,
        )

    @classmethod
    def is_steering_signal(cls, text: str) -> bool:
        """Быстрая проверка: является ли текст steering-сигналом.

        Args:
            text: Текст сообщения.

        Returns:
            True если это steering-сигнал (CONTINUE, STOP, MODIFY, и т.д.).
        """
        classifier = cls()
        command = classifier.classify(text)
        return command.signal != SteeringSignal.UNKNOWN

    @classmethod
    def get_signal(cls, text: str) -> SteeringSignal:
        """Получить тип сигнала (удобный хелпер).

        Args:
            text: Текст сообщения.

        Returns:
            SteeringSignal.
        """
        classifier = cls()
        return classifier.classify(text).signal
