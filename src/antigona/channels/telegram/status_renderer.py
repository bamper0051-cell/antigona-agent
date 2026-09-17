"""StatusRenderer — единое редактируемое статусное сообщение для каждой задачи.

Создаёт и обновляет одно сообщение в Telegram на задачу (§10 мастер-промпта).
Подписывается на EventBus для автоматического обновления при изменении статуса.

Пример сообщения::

    ⚙️ Установка Whisper

    Статус: RUNNING
    Прогресс: ▓▓▓▓▓░░░░░ 45%

    Сейчас:
    Устанавливаю системные зависимости.

    Завершено:
    ✓ Проверена версия Python
    ✓ Создано виртуальное окружение

    Следующий шаг:
    Установка openai-whisper

    Последнее обновление: 01:42:18
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, cast

from antigona.tasks.event_bus import EventBus
from antigona.tasks.models import Task, TaskEvent, TaskStatus, TaskStep

logger = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────────────────────

# Максимальная длина сообщения Telegram
MAX_MESSAGE_LENGTH = 4000

# Ширина прогресс-бара
PROGRESS_BAR_WIDTH = 10

# Цвета/иконки статусов
STATUS_ICONS: dict[str, str] = {
    "queued": "📋",
    "planning": "📝",
    "running": "⏳",
    "waiting_user": "💬",
    "waiting_confirmation": "🔒",
    "paused": "⏸",
    "retrying": "🔄",
    "failed": "❌",
    "cancelled": "🚫",
    "done": "✅",
}

# Иконки для шагов
STEP_STATUS_ICONS: dict[str, str] = {
    "pending": "⏳",
    "running": "🔄",
    "completed": "✅",
    "done": "✅",
    "success": "✅",
    "failed": "❌",
    "error": "❌",
    "skipped": "⏭",
    "retrying": "🔄",
}

# Типы событий, на которые подписываемся
STATUS_EVENT_TYPES = {
    "STEP_STARTED",
    "STEP_PROGRESS",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_RETRYING",
    "TASK_STARTED",
    "TASK_PAUSED",
    "TASK_RESUMED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_CANCELLED",
    "TASK_CREATED",
    "TASK_UPDATED",
    "PLAN_CREATED",
    "USER_INPUT_REQUIRED",
    "CONFIRMATION_REQUIRED",
}


# ── Throttle ─────────────────────────────────────────────────────────────────


class StatusThrottle:
    """Per-task throttle для предотвращения флуда в Telegram.

    - Максимум 1 обновление в 2 секунды на task_id
    - Immediate-обновления для: ошибка, запрос подтверждения, завершение
    """

    def __init__(
        self,
        min_interval: float = 2.0,
    ) -> None:
        """Инициализация throttle.

        Args:
            min_interval: Минимальный интервал между обновлениями (в секундах).
        """
        self._min_interval = min_interval
        # task_id -> timestamp последнего обновления
        self._last_update: dict[str, float] = {}

    def throttle(self, task_id: str, is_immediate: bool = False) -> bool:
        """Проверить, можно ли обновить статус прямо сейчас.

        Args:
            task_id: ID задачи.
            is_immediate: True если обновление срочное (ошибка, конфирмация).

        Returns:
            True если можно обновлять, False если слишком рано.
        """
        if is_immediate:
            # Срочные обновления не троттлим
            self._last_update[task_id] = time.time()
            return True

        now = time.time()
        last = self._last_update.get(task_id, 0.0)

        if now - last < self._min_interval:
            return False

        self._last_update[task_id] = now
        return True

    def clear(self, task_id: str | None = None) -> None:
        """Очистить историю обновлений.

        Args:
            task_id: ID задачи (если None — очистить всё).
        """
        if task_id is not None:
            self._last_update.pop(task_id, None)
        else:
            self._last_update.clear()

    @property
    def stats(self) -> dict[str, Any]:
        """Статистика троттлера."""
        return {
            "min_interval": self._min_interval,
            "task_count": len(self._last_update),
        }


# ── Рендерер ────────────────────────────────────────────────────────────────


class StatusRenderer:
    """Создаёт и обновляет единое статусное сообщение для задачи.

    Использование в боте::

        from antigona.channels.telegram.status_renderer import StatusRenderer

        renderer = StatusRenderer(bot=bot_instance)
        await renderer.create_status_message(chat_id=12345, task=task)
        await renderer.update_status_message(chat_id=12345, task=task)
    """

    def __init__(
        self,
        bot: Any,  # aiogram Bot
        throttle: StatusThrottle | None = None,
    ) -> None:
        """Инициализация рендерера статуса.

        Args:
            bot: Экземпляр aiogram Bot.
            throttle: Throttle для ограничения частоты обновлений.
                     Если None — создаётся по умолчанию.
        """
        self._bot = bot
        self._throttle = throttle or StatusThrottle()

        # Автоматическая подписка на EventBus
        self._event_subscribed = False

    # ── Подписка на события ─────────────────────────────────────────────

    async def subscribe_to_events(
        self,
        event_bus: EventBus,
    ) -> None:
        """Подписаться на события жизненного цикла задач.

        При поступлении события автоматически обновляет статусное сообщение.

        Args:
            event_bus: Экземпляр EventBus.
        """
        if self._event_subscribed:
            return

        for event_type in STATUS_EVENT_TYPES:
            event_bus.subscribe(event_type, self._on_task_event)

        # Подписка на все события (wildcard) — на случай новых типов
        event_bus.subscribe("*", self._on_task_event)

        self._event_subscribed = True
        logger.info(
            "StatusRenderer: подписан на %d типов событий",
            len(STATUS_EVENT_TYPES),
        )

    async def _on_task_event(self, event: TaskEvent) -> None:
        """Обработчик событий EventBus — обновляет статусное сообщение.

        Args:
            event: Событие жизненного цикла задачи.
        """
        # Обновляем только если есть активная задача со статусным сообщением
        task_id = event.task_id
        conversation_id = event.conversation_id

        # Проверяем throttle
        is_immediate = event.event_type in (
            "STEP_FAILED",
            "TASK_FAILED",
            "TASK_COMPLETED",
            "TASK_CANCELLED",
            "USER_INPUT_REQUIRED",
            "CONFIRMATION_REQUIRED",
            "SECURITY_EVENT",
        )

        if not self._throttle.throttle(task_id, is_immediate=is_immediate):
            return

        # Извлекаем данные из payload
        payload = event.payload or {}
        active_step: TaskStep | None = None

        if "step_id" in payload:
            # Пытаемся восстановить TaskStep из события
            active_step = self._build_step_from_event(event)

        # Используем TaskManager для получения свежей задачи
        task = await self._get_task(task_id)
        if task is None:
            logger.debug(
                "StatusRenderer: задача %s не найдена для события %s",
                task_id[:8],
                event.event_type,
            )
            return

        # Обновляем статусное сообщение
        await self.update_status_message(
            chat_id=conversation_id,
            task=task,
            active_step=active_step,
        )

    @staticmethod
    def _build_step_from_event(event: TaskEvent) -> TaskStep | None:
        """Собрать TaskStep из payload события.

        Args:
            event: Событие.

        Returns:
            TaskStep или None если данных недостаточно.
        """
        payload = event.payload or {}
        if "step_id" not in payload:
            return None

        return TaskStep(
            step_id=payload.get("step_id", ""),
            task_id=event.task_id,
            action_type=payload.get("action_type", ""),
            status=payload.get("status", "running"),
            progress=payload.get("progress", 0),
        )

    @staticmethod
    async def _get_task(task_id: str) -> Task | None:
        """Получить задачу через TaskManager.

        Args:
            task_id: ID задачи.

        Returns:
            Task или None.
        """
        try:
            from antigona.tasks.manager import TaskManager

            # Пытаемся получить TaskManager через EventBus
            bus = EventBus.get_instance()
            # Создаём временный менеджер для поиска задачи
            # (в реальном приложении менеджер должен быть синглтоном)
            # Используем импорт здесь чтобы избежать циклических зависимостей
            manager = TaskManager(event_bus=bus)
            await manager.start()
            return await manager.get_task(task_id)
        except Exception as exc:
            logger.debug(
                "StatusRenderer: не удалось получить задачу %s: %s",
                task_id[:8],
                exc,
            )
            return None

    # ── Создание статусного сообщения ────────────────────────────────────

    async def create_status_message(
        self,
        chat_id: int,
        task: Task,
        reply_to: int | None = None,
    ) -> int | None:
        """Создать начальное статусное сообщение в Telegram.

        Сохраняет message_id в task.telegram_status_message_id.

        Args:
            chat_id: ID чата Telegram.
            task: Задача.
            reply_to: ID сообщения для ответа (опционально).

        Returns:
            ID созданного сообщения или None при ошибке.
        """
        text = self.render_status(task)

        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to is not None:
                kwargs["reply_to_message_id"] = reply_to

            msg = await self._bot.send_message(**kwargs)

            # Сохраняем message_id в задаче
            task.telegram_status_message_id = msg.message_id

            # Обновляем через TaskManager
            try:
                from antigona.tasks.manager import TaskManager

                bus = EventBus.get_instance()
                manager = TaskManager(event_bus=bus)
                await manager.start()
                await manager.bind_status_message(
                    task_id=task.task_id,
                    message_id=msg.message_id,
                )
            except Exception as exc:
                logger.warning(
                    "Не удалось привязать статусное сообщение: %s",
                    exc,
                )

            logger.info(
                "Создано статусное сообщение для задачи %s (msg_id=%d)",
                task.task_id[:8],
                msg.message_id,
            )
            return cast(int, msg.message_id)

        except Exception as exc:
            logger.error(
                "Ошибка создания статусного сообщения: %s",
                exc,
            )
            return None

    # ── Обновление статусного сообщения ─────────────────────────────────

    async def update_status_message(
        self,
        chat_id: int,
        task: Task,
        active_step: TaskStep | None = None,
        suppress_errors: bool = True,
    ) -> bool:
        """Обновить существующее статусное сообщение (edit).

        Args:
            chat_id: ID чата Telegram.
            task: Задача.
            active_step: Текущий активный шаг (опционально).
            suppress_errors: Не пробрасывать ошибки Telegram (по умолчанию True).

        Returns:
            True если сообщение обновлено успешно.
        """
        message_id = task.telegram_status_message_id
        if message_id is None:
            # Нет статусного сообщения — создаём новое
            new_id = await self.create_status_message(chat_id, task)
            return new_id is not None

        text = self.render_status(task, active_step=active_step)
        if text is None:
            return False

        # Проверяем длину
        text = self._truncate(text, MAX_MESSAGE_LENGTH)

        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True

        except Exception as exc:
            error_str = str(exc).lower()

            # "Message is not modified" — не ошибка
            if "message is not modified" in error_str:
                return True

            # "Message not found" — создаём новое
            if "message not found" in error_str or "not found" in error_str:
                logger.warning(
                    "Статусное сообщение %d не найдено, создаём новое",
                    message_id,
                )
                new_id = await self.create_status_message(chat_id, task)
                return new_id is not None

            # "Can't parse entities" — пробуем без HTML
            if "can't parse entities" in error_str:
                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=self._strip_html(text),
                        disable_web_page_preview=True,
                    )
                    return True
                except Exception as fallback_err:
                    logger.warning(
                        "Fallback без HTML тоже не сработал: %s",
                        fallback_err,
                    )

            logger.error(
                "Ошибка обновления статусного сообщения: %s",
                exc,
            )

            if not suppress_errors:
                raise

            return False

    # ── Рендеринг ────────────────────────────────────────────────────────

    def render_status(
        self,
        task: Task,
        active_step: TaskStep | None = None,
    ) -> str:
        """Собрать текст статусного сообщения.

        Args:
            task: Задача.
            active_step: Текущий активный шаг (опционально).

        Returns:
            Текст сообщения (HTML).
        """
        lines: list[str] = []

        # ── Заголовок ─────────────────────────────────────────────────
        icon = STATUS_ICONS.get(task.status.value, "📋")
        title = self._escape(task.title[:80]) if task.title else "Задача"
        lines.append(f"<b>{icon} {title}</b>")
        lines.append("")

        # ── Статус и прогресс ─────────────────────────────────────────
        status_label = task.status.value.replace("_", " ").title()
        lines.append(
            f"Статус: <b>{status_label}</b>   "
            f"Риск: <b>{task.risk_level}</b>"
        )

        bar = self._render_progress_bar(
            task.progress, width=PROGRESS_BAR_WIDTH
        )
        lines.append(f"Прогресс: {bar} {task.progress}%")
        lines.append("")

        # ── Текущий шаг ──────────────────────────────────────────────
        if active_step is not None:
            lines.append("<b>Сейчас:</b>")
            step_icon = STEP_STATUS_ICONS.get(active_step.status, "⏳")
            step_text = self._format_step(active_step)
            lines.append(f"{step_icon} {step_text}")
            lines.append("")

        # ── Завершённые шаги ─────────────────────────────────────────
        completed_steps = [s for s in task.steps if s.status in (
            "completed", "done", "success"
        )]
        if completed_steps:
            lines.append("<b>Завершено:</b>")
            for step in completed_steps[-5:]:  # Последние 5
                text = self._format_step(step)
                lines.append(f"✅ {text}")
            if len(completed_steps) > 5:
                lines.append(f"    … ещё {len(completed_steps) - 5}")
            lines.append("")

        # ── Следующий шаг ────────────────────────────────────────────
        pending_steps = [s for s in task.steps if s.status == "pending"]
        if pending_steps:
            lines.append("<b>Следующий шаг:</b>")
            next_step = pending_steps[0]
            text = self._format_step(next_step)
            lines.append(text)
            lines.append("")

        # ── Результат / Ошибка (только в терминальных статусах) ──────
        if task.status.is_terminal:
            if task.status == TaskStatus.DONE and task.result:
                lines.append("<b>Результат:</b>")
                lines.append(self._truncate_text(task.result, 300))
                lines.append("")
            elif task.error:
                lines.append("<b>Ошибка:</b>")
                lines.append(
                    f"<code>{self._escape(self._truncate_text(task.error, 300))}</code>"
                )
                lines.append("")

        # ── Мета-информация ──────────────────────────────────────────
        lines.append(
            f"🆔 <code>{task.task_id[:12]}…</code>   "
            f"Последнее обновление: {self._render_time_ago(task.updated_at)}"
        )

        return "\n".join(lines)

    # ── Форматирование ──────────────────────────────────────────────────

    def _format_step(self, step: TaskStep) -> str:
        """Форматировать один шаг в читаемый текст.

        Args:
            step: Шаг задачи.

        Returns:
            Строка с описанием шага.
        """
        parts: list[str] = []

        action = step.action_type or ""
        path = step.path or ""
        command = step.command or ""

        # Определяем что показывать в зависимости от типа
        if "WRITE" in action and path:
            parts.append(f"Запись файла <code>{self._escape(path)}</code>")
        elif "SEND" in action and path:
            parts.append(f"Отправка <code>{self._escape(path)}</code>")
        elif "SHELL" in action and command:
            parts.append(
                f"Выполнение: <code>{self._escape(self._truncate_text(command, 80))}</code>"
            )
        elif "CODE" in action:
            lang = path or "python"
            parts.append(f"Выполнение кода ({lang})")
        elif "READ" in action and path:
            parts.append(f"Чтение <code>{self._escape(path)}</code>")
        elif "SEARCH" in action:
            query = step.action_data or ""
            parts.append(
                f"Поиск: <code>{self._escape(self._truncate_text(query, 60))}</code>"
            )
        elif "IMAGE" in action:
            prompt_content = step.content or step.action_data or ""
            parts.append(
                f"Генерация изображения: {self._escape(self._truncate_text(prompt_content, 60))}"
            )
        elif "KEY" in action:
            parts.append("Настройка API-ключа")
        elif "MEMORIZE" in action:
            parts.append("Запись в память")
        else:
            # Fallback на action_data
            data = step.action_data or ""
            if data:
                parts.append(
                    self._escape(self._truncate_text(data, 100))
                )
            else:
                parts.append(f"Действие {action}")

        # Добавляем прогресс если есть
        if step.progress > 0 and step.progress < 100:
            bar = self._render_progress_bar(step.progress, width=5)
            parts.append(f"[{bar} {step.progress}%]")

        return " ".join(parts)

    @staticmethod
    def _render_progress_bar(
        value: int,
        max_value: int = 100,
        width: int = 10,
    ) -> str:
        """Нарисовать прогресс-бар.

        Пример: ▓▓▓▓▓░░░░░

        Args:
            value: Текущее значение.
            max_value: Максимальное значение.
            width: Ширина бара в символах.

        Returns:
            Строка с прогресс-баром.
        """
        filled = int(width * max(0.0, min(1.0, value / max(1, max_value))))
        return "▓" * filled + "░" * (width - filled)

    @staticmethod
    def _render_time_ago(timestamp: str) -> str:
        """Форматировать время с момента последнего обновления.

        Args:
            timestamp: ISO-строка с датой-временем.

        Returns:
            Строка вида "01:42" или "2 мин назад".
        """
        if not timestamp:
            return "только что"

        try:
            dt = datetime.fromisoformat(timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            now = datetime.now(UTC).astimezone()
            diff = now - dt
            total_seconds = int(diff.total_seconds())

            if total_seconds < 10:
                return "только что"
            elif total_seconds < 60:
                return f"{total_seconds}с назад"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                return f"{minutes}мин назад"
            else:
                # Показываем время
                return dt.astimezone().strftime("%H:%M:%S")

        except (ValueError, TypeError, OSError) as exc:
            logger.debug("Ошибка парсинга timestamp %s: %s", timestamp, exc)
            return "только что"

    # ── Утилиты ───────────────────────────────────────────────────────────

    @staticmethod
    def _escape(text: str) -> str:
        """Экранировать HTML-спецсимволы.

        Args:
            text: Исходный текст.

        Returns:
            Текст с экранированными &, <, >.
        """
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _strip_html(text: str) -> str:
        """Удалить HTML-теги из текста.

        Args:
            text: Текст с HTML.

        Returns:
            Текст без HTML.
        """
        import re as _re
        return _re.sub(r"<[^>]+>", "", text).strip()

    def _truncate(self, text: str, max_length: int) -> str:
        """Обрезать текст до максимальной длины.

        Args:
            text: Исходный текст.
            max_length: Максимальная длина.

        Returns:
            Обрезанный текст.
        """
        if len(text) <= max_length:
            return text
        # Обрезаем с сохранением структуры
        cutoff = text.rfind("\n", 0, max_length - 50)
        if cutoff < max_length // 2:
            cutoff = max_length - 50
        return text[:cutoff] + (
            "\n\n<i>… сообщение обрезано "
            "(полный лог доступен в файле)</i>"
        )

    @staticmethod
    def _truncate_text(text: str | None, max_length: int) -> str:
        """Обрезать строку до максимальной длины с многоточием.

        Args:
            text: Исходный текст.
            max_length: Максимальная длина.

        Returns:
            Обрезанный текст.
        """
        if not text:
            return ""
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "…"
