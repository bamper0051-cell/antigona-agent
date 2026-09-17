"""EventBus — in-process async event system для Antigona.

Позволяет подписываться на события жизненного цикла задач
и получать уведомления асинхронно.

Архитектура:
  - EventBus — синглтон с методом publish(), который рассылает события
    всем подписчикам и сохраняет их в persistent store.
  - Подписчики — обычные callable (async def).
  - Sequence нумеруется автоматически для каждого task_id.
  - События сохраняются в JSON-файл для отладки и восстановления.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.tasks.models import TaskEvent

logger = logging.getLogger(__name__)

# Тип подписчика: асинхронная функция, принимающая TaskEvent
EventHandler = Callable[[TaskEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Асинхронная шина событий in-process.

    Использование::

        bus = EventBus.get_instance()
        bus.subscribe("TASK_CREATED", my_handler)

        await bus.publish(
            event_type="TASK_CREATED",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            payload={"title": "Моя задача"},
        )

        # Получить события задачи
        events = bus.get_events(task_id="abc123")
    """

    _instance: EventBus | None = None
    _lock = asyncio.Lock()

    def __init__(
        self,
        persist_path: str = str(paths.events_log_file()),
    ) -> None:
        self._persist_path = Path(persist_path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)

        # event_type -> [handler]
        self._subscribers: dict[str, list[EventHandler]] = {}

        # event_type -> [handler] (wildcard — все события)
        self._wildcard_subscribers: list[EventHandler] = []

        # task_id -> sequence counter
        self._sequences: dict[str, int] = {}

        # task_id -> [события] (in-memory кэш, последние 1000)
        self._events_cache: dict[str, list[dict[str, Any]]] = {}

        # Глобальная нумерация событий (для SSE cursor)
        self._global_seq: int = 0
        # Кольцевой буфер всех событий в глобальном порядке (последние 10000)
        self._all_events: deque[dict[str, Any]] = deque(maxlen=10000)

        # Загружаем историю событий
        self._load_persisted()

    # ── Singleton ───────────────────────────────────────────────────────────

    @classmethod
    def get_instance(
        cls,
        persist_path: str = str(paths.events_log_file()),
    ) -> EventBus:
        """Получить глобальный экземпляр EventBus."""
        if cls._instance is None:
            cls._instance = cls(persist_path=persist_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Сбросить синглтон (для тестов)."""
        cls._instance = None

    # ── Подписка / Отписка ─────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
    ) -> None:
        """Подписаться на событие определённого типа.

        Args:
            event_type: Тип события (например "TASK_CREATED").
                       Передайте "*" для подписки на ВСЕ события.
            handler: Асинхронная функция-обработчик.
        """
        if event_type == "*":
            self._wildcard_subscribers.append(handler)
            logger.debug(
                "Подписчик %s зарегистрирован на все события",
                handler.__name__,
            )
            return

        self._subscribers.setdefault(event_type, []).append(handler)
        logger.debug(
            "Подписчик %s зарегистрирован на событие %s",
            handler.__name__,
            event_type,
        )

    def unsubscribe(
        self,
        event_type: str,
        handler: EventHandler,
    ) -> None:
        """Отписаться от события.

        Args:
            event_type: Тип события.
            handler: Функция-обработчик для удаления.
        """
        if event_type == "*":
            if handler in self._wildcard_subscribers:
                self._wildcard_subscribers.remove(handler)
            return

        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug(
                "Подписчик %s отписан от события %s",
                handler.__name__,
                event_type,
            )

    def clear_subscribers(self) -> None:
        """Удалить всех подписчиков (для тестов/перезагрузки)."""
        self._subscribers.clear()
        self._wildcard_subscribers.clear()
        logger.debug("Все подписчики EventBus удалены")

    # ── Публикация ─────────────────────────────────────────────────────────

    async def publish(
        self,
        event_type: str,
        task_id: str,
        conversation_id: int,
        payload: dict[str, Any] | None = None,
        source: str = "core",
    ) -> TaskEvent:
        """Опубликовать событие.

        Автоматически:
        - Генерирует event_id и timestamp
        - Инкрементирует sequence для task_id
        - Сохраняет в persistent store
        - Рассылает подписчикам

        Args:
            event_type: Тип события (из TASK_EVENT_TYPES или кастомный).
            task_id: ID задачи.
            conversation_id: Telegram chat ID.
            payload: Произвольные данные события.
            source: Источник события ("core", "user", "system").

        Returns:
            Созданный TaskEvent.
        """
        # Sequence auto-increment
        async with self._lock:
            seq = self._sequences.get(task_id, 0) + 1
            self._sequences[task_id] = seq

        event = TaskEvent(
            event_type=event_type,
            task_id=task_id,
            conversation_id=conversation_id,
            source=source,
            sequence=seq,
            payload=payload or {},
        )

        # Сохраняем в кэш, кольцевой буфер и persistent store
        async with self._lock:
            self._global_seq += 1
            event_dict = event.to_dict()
            event_dict["_global_seq"] = self._global_seq

            self._events_cache.setdefault(task_id, []).append(event_dict)
            self._all_events.append(event_dict)
            # Держим не более 1000 событий на задачу в кэше
            if len(self._events_cache[task_id]) > 1000:
                self._events_cache[task_id] = self._events_cache[task_id][-1000:]
            self._save_event_dict(event_dict)

        # Рассылаем подписчикам
        await self._dispatch(event)

        logger.debug(
            "Событие %s [seq=%d] для задачи %s опубликовано",
            event_type,
            seq,
            task_id,
        )
        return event

    async def _dispatch(self, event: TaskEvent) -> None:
        """Разослать событие всем подписчикам.

        Ошибки в обработчиках не роняют шину — логируются.
        """
        handlers: list[EventHandler] = []

        # Подписчики на конкретный тип
        type_handlers = self._subscribers.get(event.event_type, [])
        handlers.extend(type_handlers)

        # Wildcard-подписчики
        handlers.extend(self._wildcard_subscribers)

        if not handlers:
            return

        results = await asyncio.gather(
            *[self._safe_call(h, event) for h in handlers],
            return_exceptions=True,
        )

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(
                    "Ошибка в обработчике %s для события %s: %s",
                    handlers[i].__name__,
                    event.event_type,
                    r,
                )

    @staticmethod
    async def _safe_call(
        handler: EventHandler,
        event: TaskEvent,
    ) -> None:
        """Вызвать обработчик с логированием ошибок."""
        try:
            await handler(event)
        except Exception as exc:
            logger.exception(
                "Исключение в обработчике %s: %s",
                handler.__name__,
                exc,
            )
            # Не пробрасываем — шина не должна падать

    # ── Чтение событий ─────────────────────────────────────────────────────

    def get_events(
        self,
        task_id: str,
        limit: int = 50,
        offset: int = 0,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Получить события задачи.

        Args:
            task_id: ID задачи.
            limit: Максимум событий.
            offset: Смещение от начала.
            event_type: Фильтр по типу (опционально).

        Returns:
            Список событий (последние — в конце).
        """
        events = self._events_cache.get(task_id, [])
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]
        return events[offset : offset + limit]

    def get_last_event(
        self,
        task_id: str,
        event_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Получить последнее событие задачи.

        Args:
            task_id: ID задачи.
            event_type: Фильтр по типу (опционально).

        Returns:
            Последнее событие или None.
        """
        events = self.get_events(task_id, limit=1000, event_type=event_type)
        return events[-1] if events else None

    def count_events(self, task_id: str) -> int:
        """Количество событий задачи."""
        return len(self._events_cache.get(task_id, []))

    # ── SSE Streaming ────────────────────────────────────────────────────

    async def stream_events_since(
        self,
        cursor: str = "",
        conversation_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """Асинхронный генератор событий для SSE — исторический срез.

        Отдаёт все события из кольцевого буфера (последние 10000),
        начиная с cursor, опционально фильтруя по conversation_id.

        Args:
            cursor: Глобальный sequence, после которого отдавать события
                    (пустая строка = все события).
            conversation_id: Фильтр по conversation_id (пустая строка = все).

        Yields:
            Словари событий в формате SSE data.
        """
        cursor_seq = int(cursor) if cursor else 0
        for event_dict in list(self._all_events):
            seq = int(event_dict.get("_global_seq", 0))
            if seq <= cursor_seq:
                continue
            if conversation_id:
                ev_conv = event_dict.get("conversation_id")
                if ev_conv is not None and str(ev_conv) != str(conversation_id):
                    continue
            yield event_dict

    # ── Статистика ─────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Статистика шины."""
        return {
            "subscribers_count": sum(len(h) for h in self._subscribers.values())
            + len(self._wildcard_subscribers),
            "event_types": list(self._subscribers.keys()),
            "tasks_tracked": len(self._events_cache),
            "total_events": sum(len(e) for e in self._events_cache.values()),
            "persist_path": str(self._persist_path),
        }

    # ── Persistence ─────────────────────────────────────────────────────────

    def _save_event_dict(self, event_dict: dict[str, Any]) -> None:
        """Сохранить одно событие (dict) в JSONL-файл.

        Формат: одна JSON-строка на строку.
        """
        try:
            line = json.dumps(event_dict, ensure_ascii=False, default=str)
            with open(self._persist_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("Ошибка сохранения события в %s: %s", self._persist_path, exc)
            # Не роняем шину из-за ошибки I/O

    def _load_persisted(self) -> None:
        """Загрузить события из JSONL-файла при старте.

        Восстанавливает кэш, sequence счётчики, глобальную нумерацию
        и кольцевой буфер _all_events.
        """
        if not self._persist_path.exists():
            return

        try:
            with open(self._persist_path, encoding="utf-8") as f:
                for line_idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # Присваиваем глобальный seq на основе позиции в файле,
                        # если поле ещё не записано (старый формат).
                        data.setdefault("_global_seq", line_idx + 1)
                        self._global_seq = max(self._global_seq, int(data["_global_seq"]))
                        self._all_events.append(data)

                        tid = data.get("task_id", "")
                        if tid:
                            self._events_cache.setdefault(tid, []).append(data)
                            seq = data.get("sequence", 0)
                            if seq > self._sequences.get(tid, 0):
                                self._sequences[tid] = seq
                    except json.JSONDecodeError:
                        continue
            logger.info(
                "EventBus: загружено событий для %d задач из %s (global_seq=%d)",
                len(self._events_cache),
                self._persist_path,
                self._global_seq,
            )
        except OSError as exc:
            logger.error("Ошибка загрузки событий: %s", exc)

    def clear_persisted(self) -> None:
        """Очистить persistent store событий."""
        try:
            if self._persist_path.exists():
                self._persist_path.unlink()
            self._events_cache.clear()
            self._sequences.clear()
            logger.info("EventBus: persistent store очищен")
        except OSError as exc:
            logger.error("Ошибка очистки событий: %s", exc)

    def rebuild_cache_from_store(self) -> None:
        """Перестроить in-memory кэш из persistent store."""
        self._events_cache.clear()
        self._sequences.clear()
        self._load_persisted()
