"""TaskManager — CRUD для задач Antigona с событийной системой.

Заменяет/расширяет task_monitor.py и task/runtime.py.

Архитектура:
  - Хранит задачи in-memory + JSON persist в .tasks/tasks.json
  - Каждое изменение задачи публикует событие через EventBus
  - Полностью async (asyncio-based)
  - Thread-safe через asyncio.Lock

Usage::

    bus = EventBus.get_instance()
    manager = TaskManager(event_bus=bus)
    await manager.start()  # загружает сохранённые задачи

    task = await manager.create_task(
        conversation_id=12345,
        owner_user_id=67890,
        title="Создать файл",
        original_request="создай файл test.txt",
    )
    await manager.add_step(task.task_id, {"action_type": "WRITE_FILE", ...})
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.tasks.models import Task, TaskStatus, TaskStep

logger = logging.getLogger(__name__)

# Путь к JSON-файлу для персистентности задач
TASKS_PERSIST_PATH = paths.tasks_state_file()


class TaskManager:
    """Менеджер задач Antigona.

    Attributes:
        event_bus: Экземпляр EventBus для публикации событий.
        tasks_dir: Директория для persist задач.
    """

    def __init__(
        self,
        event_bus: Any | None = None,  # EventBus, но избегаем циклического импорта
        tasks_persist_path: str | Path = TASKS_PERSIST_PATH,
    ) -> None:
        self._event_bus = event_bus
        self._persist_path = Path(tasks_persist_path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)

        # task_id -> Task
        self._tasks: dict[str, Task] = {}

        # conversation_id -> [task_id] (индекс для быстрого поиска)
        self._by_conversation: dict[int, list[str]] = {}

        # (conversation_id, telegram_status_message_id) -> task_id (индекс для Reply)
        self._by_message: dict[tuple[int, int], str] = {}

        # Блокировка для thread-safe доступа
        self._lock = asyncio.Lock()

        # Флаг загрузки
        self._loaded = False

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Загрузить задачи из персистентного хранилища.

        Вызвать при старте приложения, после создания EventBus.
        """
        if self._loaded:
            return
        await self._load_persisted()
        self._loaded = True
        logger.info(
            "TaskManager: загружено %d задач из %s",
            len(self._tasks),
            self._persist_path,
        )

    async def stop(self) -> None:
        """Сохранить все задачи перед остановкой."""
        await self._save_persisted()
        logger.info("TaskManager: сохранено %d задач", len(self._tasks))

    # ── CRUD: Create ────────────────────────────────────────────────────────

    async def create_task(
        self,
        conversation_id: int,
        owner_user_id: int,
        title: str = "",
        original_request: str = "",
        current_request: str = "",
        risk_level: str = "LOW",
        priority: int = 5,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Создать новую задачу.

        Args:
            conversation_id: Telegram chat ID.
            owner_user_id: ID пользователя-владельца.
            title: Заголовок задачи.
            original_request: Исходный запрос пользователя.
            current_request: Текущий (уточнённый) запрос.
            risk_level: Уровень риска (LOW/MEDIUM/HIGH/CRITICAL).
            priority: Приоритет 0–10.
            metadata: Произвольные метаданные.

        Returns:
            Созданная задача (Task).
        """
        task = Task(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            title=title or original_request[:80],
            original_request=original_request,
            current_request=current_request or original_request,
            status=TaskStatus.QUEUED,
            risk_level=risk_level.upper(),
            priority=max(0, min(10, priority)),
            metadata=metadata or {},
        )

        async with self._lock:
            self._tasks[task.task_id] = task
            self._by_conversation.setdefault(conversation_id, []).append(task.task_id)
            await self._save_persisted()

        # Публикуем событие
        await self._publish_event(
            event_type="TASK_CREATED",
            task=task,
            payload={
                "title": task.title,
                "original_request": task.original_request,
                "risk_level": task.risk_level,
                "priority": task.priority,
            },
        )

        logger.info(
            "Создана задача %s: %s (conv=%d)",
            task.task_id[:8],
            task.title[:50],
            conversation_id,
        )
        return task

    # ── CRUD: Read ──────────────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Task | None:
        """Получить задачу по ID.

        Args:
            task_id: UUID задачи.

        Returns:
            Task или None.
        """
        async with self._lock:
            return self._tasks.get(task_id)

    async def get_active_tasks(
        self,
        conversation_id: int,
    ) -> list[Task]:
        """Получить активные задачи для диалога.

        Args:
            conversation_id: Telegram chat ID.

        Returns:
            Список активных (не терминальных) задач.
        """
        async with self._lock:
            ids = self._by_conversation.get(conversation_id, [])
            result: list[Task] = []
            for tid in ids:
                task = self._tasks.get(tid)
                if task and not task.status.is_terminal:
                    result.append(task)
            return result

    async def get_all_tasks(
        self,
        conversation_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """Получить все задачи (с фильтрацией и пагинацией).

        Args:
            conversation_id: Опциональный фильтр по диалогу.
            limit: Максимум задач.
            offset: Смещение.

        Returns:
            Список задач, сортировка по created_at (новые в начале).
        """
        async with self._lock:
            tasks = list(self._tasks.values())
            if conversation_id is not None:
                tasks = [t for t in tasks if t.conversation_id == conversation_id]
            # Сортируем по created_at (новые — первые)
            tasks.sort(key=lambda t: t.created_at, reverse=True)
            return tasks[offset : offset + limit]

    async def get_tasks_by_status(
        self,
        status: TaskStatus | str,
        conversation_id: int | None = None,
    ) -> list[Task]:
        """Получить задачи по статусу.

        Args:
            status: Статус (TaskStatus или строка).
            conversation_id: Опциональный фильтр по диалогу.

        Returns:
            Список задач.
        """
        if isinstance(status, str):
            status = TaskStatus(status)

        async with self._lock:
            result: list[Task] = []
            for task in self._tasks.values():
                if task.status != status:
                    continue
                if conversation_id is not None and task.conversation_id != conversation_id:
                    continue
                result.append(task)
            return result

    async def get_recoverable_tasks(self) -> list[Task]:
        """Получить задачи, которые можно восстановить после перезапуска.

        Это задачи с активными статусами (queued, planning, running, retrying)
        и задачи, ожидающие внешнего действия.

        Returns:
            Список восстанавливаемых задач.
        """
        async with self._lock:
            result: list[Task] = []
            for task in self._tasks.values():
                if task.status.is_active or task.status.is_waiting:
                    result.append(task)
            return result

    async def find_task_by_message(
        self,
        conversation_id: int,
        message_id: int,
    ) -> Task | None:
        """Найти задачу по ID сообщения со статусом.

        Используется для ответа на сообщение-статус задачи (Reply).

        Args:
            conversation_id: Telegram chat ID.
            message_id: ID сообщения со статусом.

        Returns:
            Task или None.
        """
        async with self._lock:
            tid = self._by_message.get((conversation_id, message_id))
            if tid:
                return self._tasks.get(tid)
            return None

    async def get_active_tasks_by_request(
        self,
        request_text: str,
    ) -> list[Task]:
        """Найти активные задачи по тексту запроса.

        Используется для контекстной привязки: если пользователь пишет
        "продолжи", ищем задачу с похожим original_request.

        Args:
            request_text: Текст запроса.

        Returns:
            Список подходящих задач.
        """
        if not request_text:
            return []

        request_lower = request_text.lower().strip()
        if not request_lower:
            return []

        async with self._lock:
            result: list[Task] = []
            for task in self._tasks.values():
                if task.status.is_terminal:
                    continue
                # Поиск по original_request, current_request, title
                source = (
                    (task.original_request + " " + task.current_request + " " + task.title)
                    .lower()
                )
                if request_lower in source or any(
                    word in source for word in request_lower.split()
                ):
                    result.append(task)
            return result[:10]  # Не более 10

    # ── CRUD: Update ────────────────────────────────────────────────────────

    async def update_task(
        self,
        task_id: str,
        **kwargs: Any,
    ) -> Task | None:
        """Обновить поля задачи.

        Args:
            task_id: UUID задачи.
            **kwargs: Поля для обновления (любые атрибуты Task).

        Returns:
            Обновлённая задача или None (не найдена).
        """
        # Нельзя обновлять task_id
        kwargs.pop("task_id", None)

        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("Задача %s не найдена для обновления", task_id[:8])
                return None

            # Обновляем атрибуты
            changed: list[str] = []
            for key, value in kwargs.items():
                if hasattr(task, key):
                    old_val = getattr(task, key)
                    # Сравниваем значения
                    if key == "steps" and isinstance(value, list):
                        # steps обрабатываем отдельно
                        continue
                    if old_val != value:
                        setattr(task, key, value)
                        changed.append(key)
                else:
                    logger.warning("Попытка обновить неизвестное поле %s", key)

            if changed:
                task.updated_at = datetime.now(UTC).isoformat()

                # Если статус изменился — обновляем метки времени
                if "status" in kwargs:
                    await self._on_status_change(task)

                # Обновляем индекс по message_id
                if task.telegram_status_message_id is not None:
                    msg_key = (task.conversation_id, task.telegram_status_message_id)
                    self._by_message[msg_key] = task.task_id

                # Сохраняем
                await self._save_persisted()

        # Публикуем событие (вне блокировки)
        if changed:
            event_type = "TASK_UPDATED"
            if "status" in kwargs:
                status_map = {
                    TaskStatus.RUNNING: "TASK_STARTED",
                    TaskStatus.PAUSED: "TASK_PAUSED",
                    TaskStatus.DONE: "TASK_COMPLETED",
                    TaskStatus.FAILED: "TASK_FAILED",
                    TaskStatus.CANCELLED: "TASK_CANCELLED",
                }
                event_type = status_map.get(kwargs["status"], event_type)

            await self._publish_event(
                event_type=event_type,
                task=task,
                payload={"changed_fields": changed, "updates": kwargs},
            )

            logger.debug(
                "Задача %s обновлена: %s", task_id[:8], ", ".join(changed)
            )

        return task

    async def _on_status_change(self, task: Task) -> None:
        """Обработать смену статуса — обновить метки времени."""
        now = datetime.now(UTC).isoformat()
        if task.status == TaskStatus.RUNNING and task.started_at is None:
            task.started_at = now
        if task.status.is_terminal and task.finished_at is None:
            task.finished_at = now

    # ── Steps ───────────────────────────────────────────────────────────────

    async def add_step(
        self,
        task_id: str,
        action_type: str = "",
        action_data: str = "",
        path: str | None = None,
        content: str | None = None,
        command: str | None = None,
    ) -> TaskStep | None:
        """Добавить шаг к задаче.

        Args:
            task_id: ID задачи.
            action_type: Тип действия.
            action_data: Данные действия.
            path: Путь к файлу.
            content: Содержимое.
            command: Команда оболочки.

        Returns:
            Созданный TaskStep или None (задача не найдена).
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("Задача %s не найдена для добавления шага", task_id[:8])
                return None

            step = TaskStep(
                task_id=task_id,
                action_type=action_type,
                action_data=action_data,
                path=path,
                content=content,
                command=command,
                status="pending",
            )
            task.steps.append(step)
            task.updated_at = datetime.now(UTC).isoformat()
            await self._save_persisted()

        # Публикуем событие
        await self._publish_event(
            event_type="TASK_UPDATED",
            task=task,
            payload={
                "action": "step_added",
                "step_id": step.step_id,
                "action_type": action_type,
            },
        )
        return step

    async def update_step(
        self,
        step_id: str,
        **kwargs: Any,
    ) -> TaskStep | None:
        """Обновить шаг задачи.

        Args:
            step_id: UUID шага.
            **kwargs: Поля для обновления.

        Returns:
            Обновлённый TaskStep или None.
        """
        step, task = await self._find_step(step_id)
        if step is None:
            logger.warning("Шаг %s не найден", step_id[:8])
            return None

        changed: list[str] = []
        for key, value in kwargs.items():
            if hasattr(step, key) and key != "step_id":
                old_val = getattr(step, key)
                if old_val != value:
                    setattr(step, key, value)
                    changed.append(key)

        if changed and task is not None:
            task.updated_at = datetime.now(UTC).isoformat()
            await self._save_persisted()

            # Публикуем событие шага
            event_type_map = {
                "running": "STEP_STARTED",
                "completed": "STEP_COMPLETED",
                "failed": "STEP_FAILED",
                "retrying": "STEP_RETRYING",
            }
            status_event = None
            if "status" in kwargs:
                step_status = str(kwargs["status"])
                status_event = event_type_map.get(step_status)

            if status_event:
                await self._publish_event(
                    event_type=status_event,
                    task=task,
                    payload={
                        "step_id": step.step_id,
                        "action_type": step.action_type,
                        "progress": step.progress,
                        "status": step.status,
                    },
                )
            else:
                await self._publish_event(
                    event_type="STEP_PROGRESS",
                    task=task,
                    payload={
                        "step_id": step.step_id,
                        "action_type": step.action_type,
                        "progress": step.progress,
                        "changed_fields": changed,
                    },
                )

        return step

    async def get_steps(self, task_id: str) -> list[TaskStep]:
        """Получить шаги задачи.

        Args:
            task_id: ID задачи.

        Returns:
            Список TaskStep.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return []
            return list(task.steps)

    async def get_step(self, step_id: str) -> TaskStep | None:
        """Получить шаг по ID.

        Args:
            step_id: UUID шага.

        Returns:
            TaskStep или None.
        """
        step, _ = await self._find_step(step_id)
        return step

    async def _find_step(
        self,
        step_id: str,
    ) -> tuple[TaskStep | None, Task | None]:
        """Найти шаг по ID во всех задачах.

        Returns:
            (TaskStep, Task) или (None, None).
        """
        async with self._lock:
            for task in self._tasks.values():
                for step in task.steps:
                    if step.step_id == step_id:
                        return step, task
        return None, None

    # ── Status transitions ──────────────────────────────────────────────────

    async def start_task(self, task_id: str) -> Task | None:
        """Перевести задачу в статус RUNNING.

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        return await self.update_task(task_id, status=TaskStatus.RUNNING)

    async def pause_task(self, task_id: str) -> Task | None:
        """Приостановить задачу.

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        return await self.update_task(task_id, status=TaskStatus.PAUSED)

    async def resume_task(self, task_id: str) -> Task | None:
        """Возобновить задачу.

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        return await self.update_task(task_id, status=TaskStatus.RUNNING)

    async def cancel_task(self, task_id: str) -> Task | None:
        """Отменить задачу.

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        return await self.update_task(task_id, status=TaskStatus.CANCELLED)

    async def complete_task(
        self,
        task_id: str,
        result: str | None = None,
    ) -> Task | None:
        """Завершить задачу успешно.

        ВНИМАНИЕ: Этот метод НЕЛЬЗЯ вызывать напрямую.
        Только Verifier имеет право переводить задачу в DONE.
        Используйте Gateway → Verifier для успешного завершения.

        Raises:
            RuntimeError: Всегда — use Gateway + Verifier for DONE.
        """
        raise RuntimeError(
            "Use Gateway + Verifier for DONE. "
            "TaskManager.complete_task() is disabled — "
            "only the Verifier component may transition a task to DONE. "
            "See AGENTS.md: 'Только Verifier вправе переводить task в DONE'."
        )

    async def _complete_task_internal(self, task_id: str, result: str | None = None) -> Task | None:
        """Внутренний метод для тестов/миграции. Не использовать в production."""
        from datetime import UTC, datetime
        return await self.update_task(
            task_id, status=TaskStatus.DONE,
            finished_at=datetime.now(UTC).isoformat(),
            progress=100, result=result,
        )

    async def fail_task(
        self,
        task_id: str,
        error: str | None = None,
    ) -> Task | None:
        """Отметить задачу как неудачную.

        Args:
            task_id: ID задачи.
            error: Сообщение об ошибке.

        Returns:
            Task или None.
        """
        return await self.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=error,
        )

    async def retry_task(self, task_id: str) -> Task | None:
        """Повторить задачу после ошибки.

        Сбрасывает шаги со статусом "failed" обратно в "pending".

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None

            task.status = TaskStatus.RETRYING
            task.updated_at = datetime.now(UTC).isoformat()
            task.error = None

            # Сбрасываем упавшие шаги
            for step in task.steps:
                if step.status in ("failed", "error"):
                    step.status = "pending"
                    step.error = None
                    step.retry_count += 1

            await self._save_persisted()

        await self._publish_event(
            event_type="TASK_UPDATED",
            task=task,
            payload={"action": "retry", "previous_status": "failed"},
        )
        return task

    async def set_progress(
        self,
        task_id: str,
        progress: int,
    ) -> Task | None:
        """Установить прогресс задачи.

        Args:
            task_id: ID задачи.
            progress: Прогресс 0–100.

        Returns:
            Task или None.
        """
        progress = max(0, min(100, progress))
        return await self.update_task(task_id, progress=progress)

    # ── Plan ────────────────────────────────────────────────────────────────

    async def set_plan(
        self,
        task_id: str,
        plan: str,
    ) -> Task | None:
        """Установить план задачи.

        Args:
            task_id: ID задачи.
            plan: Текст плана.

        Returns:
            Task или None.
        """
        task = await self.update_task(task_id, plan=plan, status=TaskStatus.PLANNING)
        if task:
            await self._publish_event(
                event_type="PLAN_CREATED",
                task=task,
                payload={"plan_preview": plan[:200]},
            )
        return task

    # ── Status message binding ─────────────────────────────────────────────

    async def bind_status_message(
        self,
        task_id: str,
        message_id: int,
    ) -> Task | None:
        """Привязать ID сообщения статуса к задаче.

        Позволяет находить задачу по Reply на сообщение-статус.

        Args:
            task_id: ID задачи.
            message_id: ID сообщения Telegram со статусом.

        Returns:
            Task или None.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None

            # Удаляем старую привязку, если была
            old_mid = task.telegram_status_message_id
            if old_mid is not None:
                old_key = (task.conversation_id, old_mid)
                self._by_message.pop(old_key, None)

            task.telegram_status_message_id = message_id
            new_key = (task.conversation_id, message_id)
            self._by_message[new_key] = task.task_id

            task.updated_at = datetime.now(UTC).isoformat()
            await self._save_persisted()

        return task

    # ── Steer ───────────────────────────────────────────────────────────────

    async def steer_task(
        self,
        task_id: str,
        new_request: str,
    ) -> Task | None:
        """Обновить запрос задачи (steering).

        Args:
            task_id: ID задачи.
            new_request: Новый/уточнённый запрос.

        Returns:
            Task или None.
        """
        old_request: str = ""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            old_request = task.current_request
            task.current_request = new_request
            task.updated_at = datetime.now(UTC).isoformat()
            await self._save_persisted()

        await self._publish_event(
            event_type="TASK_STEERED",
            task=task,
            payload={
                "old_request": old_request,
                "new_request": new_request,
            },
        )
        return task

    # ── Delete ──────────────────────────────────────────────────────────────

    async def delete_task(self, task_id: str) -> bool:
        """Полностью удалить задачу.

        Args:
            task_id: ID задачи.

        Returns:
            True если задача удалена, False если не найдена.
        """
        async with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                return False

            # Удаляем из индексов
            conv_id = task.conversation_id
            conv_tasks = self._by_conversation.get(conv_id, [])
            if task.task_id in conv_tasks:
                conv_tasks.remove(task.task_id)

            if task.telegram_status_message_id is not None:
                msg_key = (conv_id, task.telegram_status_message_id)
                self._by_message.pop(msg_key, None)

            await self._save_persisted()

        logger.info("Задача %s удалена", task_id[:8])
        return True

    # ── Statistics ──────────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        """Статистика менеджера задач.

        Returns:
            Словарь со статистикой.
        """
        async with self._lock:
            total = len(self._tasks)
            by_status: dict[str, int] = {}
            by_conv: dict[int, int] = {}
            active = 0
            for task in self._tasks.values():
                status_key = task.status.value
                by_status[status_key] = by_status.get(status_key, 0) + 1
                by_conv[task.conversation_id] = by_conv.get(task.conversation_id, 0) + 1
                if not task.status.is_terminal:
                    active += 1

            return {
                "total_tasks": total,
                "active_tasks": active,
                "by_status": by_status,
                "by_conversation": by_conv,
                "conversations_count": len(by_conv),
            }

    # ── Rendering ───────────────────────────────────────────────────────────

    def render_progress(self, task: Task, width: int = 10) -> str:
        """Нарисовать прогресс-бар задачи.

        Args:
            task: Задача.
            width: Ширина бара.

        Returns:
            Строка с прогресс-баром.
        """
        filled = int(width * max(0.0, min(1.0, task.progress / 100)))
        return "▓" * filled + "░" * (width - filled)

    def render_short(self, task: Task) -> str:
        """Краткое текстовое представление задачи.

        Returns:
            Строка вида "📋 Создать файл [▓▓▓▓▓░░░░░] 50% • running".
        """
        icon = {
            TaskStatus.QUEUED: "📋",
            TaskStatus.PLANNING: "📝",
            TaskStatus.RUNNING: "⏳",
            TaskStatus.WAITING_USER: "💬",
            TaskStatus.WAITING_CONFIRMATION: "🔒",
            TaskStatus.PAUSED: "⏸",
            TaskStatus.RETRYING: "🔄",
            TaskStatus.FAILED: "❌",
            TaskStatus.CANCELLED: "🚫",
            TaskStatus.DONE: "✅",
        }.get(task.status, "📋")

        bar = self.render_progress(task)
        return (
            f"{icon} {task.title[:50]} "
            f"[{bar}] {task.progress}% • {task.status.value}"
        )

    # ── Event publishing ────────────────────────────────────────────────────

    async def _publish_event(
        self,
        event_type: str,
        task: Task,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Опубликовать событие через EventBus (если он установлен).

        Args:
            event_type: Тип события.
            task: Задача, к которой относится событие.
            payload: Дополнительные данные.
        """
        if self._event_bus is None:
            return

        try:
            await self._event_bus.publish(
                event_type=event_type,
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                payload=payload or {},
            )
        except Exception as exc:
            logger.warning(
                "Не удалось опубликовать событие %s: %s",
                event_type,
                exc,
            )

    # ── Persistence ─────────────────────────────────────────────────────────

    async def _save_persisted(self) -> None:
        """Сохранить все задачи в JSON-файл.

        Формат:
        {
            "tasks": {task_id: task_dict, ...},
            "indexes": {
                "by_message": {"conv_msg_key": task_id, ...},
                "by_conversation": {"conv_id": [task_id, ...], ...}
            }
        }
        """
        try:
            data: dict[str, Any] = {
                "tasks": {
                    tid: task.to_dict() for tid, task in self._tasks.items()
                },
                "indexes": {
                    "by_message": {
                        f"{cid}_{mid}": tid
                        for (cid, mid), tid in self._by_message.items()
                    },
                    "by_conversation": {
                        str(cid): tids
                        for cid, tids in self._by_conversation.items()
                    },
                },
            }
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str)
            )
            self._persist_path.chmod(0o600)
        except OSError as exc:
            logger.error("Ошибка сохранения задач в %s: %s", self._persist_path, exc)

    async def _load_persisted(self) -> None:
        """Загрузить задачи из JSON-файла."""
        if not self._persist_path.exists():
            return

        try:
            raw = self._persist_path.read_text()
            if not raw.strip():
                return
            data = json.loads(raw)

            # Загружаем задачи
            tasks_data = data.get("tasks", {})
            for tid, task_dict in tasks_data.items():
                task = Task.from_dict(task_dict)
                self._tasks[tid] = task

            # Восстанавливаем индексы
            indexes = data.get("indexes", {})
            by_msg = indexes.get("by_message", {})
            for key, tid in by_msg.items():
                try:
                    parts = key.split("_", 1)
                    if len(parts) == 2:
                        cid = int(parts[0])
                        mid = int(parts[1])
                        self._by_message[(cid, mid)] = tid
                except (ValueError, KeyError):
                    continue

            by_conv = indexes.get("by_conversation", {})
            for cid_str, tids in by_conv.items():
                try:
                    cid = int(cid_str)
                    self._by_conversation[cid] = tids
                except (ValueError, KeyError):
                    continue

            logger.info(
                "Загружено %d задач, %d диалогов из %s",
                len(self._tasks),
                len(self._by_conversation),
                self._persist_path,
            )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Ошибка загрузки задач из %s: %s", self._persist_path, exc)
