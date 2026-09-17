"""ContextAdapter — мост между Telegram-обработчиком и ContextResolver.

Преобразует сырые Telegram-апдейты (Message, EditedMessage) в ResolvedContext,
затем маршрутизирует их в TaskRunner или ConversationEngine.

Не модифицирует bot.py — подключается как отдельный модуль.

Usage (в bot.py или отдельном регистраторе)::

    from antigona.channels.telegram.context_adapter import ContextAdapter

    adapter = ContextAdapter(
        bot=bot_instance,
        task_manager=task_manager,
        event_bus=event_bus,
        summarizer=memory_summarizer,
    )

    # В обработчике text_handler:
    ctx = await adapter.handle_text_message(message)
    if ctx.intent in (IntentType.NEW_TASK, IntentType.ADD_REQUIREMENT):
        await adapter.route_to_task_runner(ctx, message)
    elif ctx.intent == IntentType.STATUS_QUERY:
        await adapter.route_status_query(ctx, message)
    elif ctx.intent in (IntentType.CONFIRM, IntentType.STEER_EXISTING):
        await adapter.route_steering(ctx, message)
    elif ctx.intent in (IntentType.CANCEL, IntentType.PAUSE, IntentType.RESUME, IntentType.RETRY):
        await adapter.route_control(ctx, message)

    # В обработчике edited_message:
    ctx = await adapter.handle_edit(edited_message)
    await adapter.route_edit(ctx, edited_message)
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.types import Message

from antigona.core.control_plane import SteeringCommand

# GatewayClient — единый способ управления задачами через Gateway
from antigona.core.gateway_client import GatewayClient
from antigona.tasks.context_resolver import (
    ContextResolver,
    IntentType,
    ReplyMappingStore,
    ResolvedContext,
)
from antigona.tasks.event_bus import EventBus
from antigona.tasks.manager import TaskManager
from antigona.tasks.steering import SteeringClassifier, SteeringQueue, SteeringSignal

logger = logging.getLogger(__name__)


class ContextAdapter:
    """Мост между Telegram message handler и ContextResolver.

    Принимает сырые Telegram Message/EditedMessage,
    преобразует через ContextResolver, затем маршрутизирует
    в TaskRunner или ConversationEngine.

    Attributes:
        resolver: Экземпляр ContextResolver.
        steering_queue: Очередь steering-команд.
        task_manager: TaskManager для операций над задачами.
        event_bus: EventBus для публикации событий.
        summarizer: MemorySummarizer (опционально).
    """

    def __init__(
        self,
        task_manager: TaskManager,
        event_bus: EventBus | None = None,
        reply_store: ReplyMappingStore | None = None,
        steering_queue: SteeringQueue | None = None,
        summarizer: Any | None = None,
        gateway_client: GatewayClient | None = None,
    ) -> None:
        """
        Args:
            task_manager: TaskManager для поиска/обновления задач.
            event_bus: EventBus для публикации событий (опционально).
            reply_store: Хранилище привязок (опционально, создаётся по умолчанию).
            steering_queue: Очередь steering (опционально, создаётся по умолчанию).
            summarizer: MemorySummarizer (опционально).
            gateway_client: GatewayClient для управления задачами через Gateway.
        """
        self.task_manager = task_manager
        self.event_bus = event_bus
        self.summarizer = summarizer
        self.gateway_client: GatewayClient = gateway_client or GatewayClient()

        self._reply_store = reply_store or ReplyMappingStore()
        self._steering_queue = steering_queue or SteeringQueue()
        self.resolver = ContextResolver(
            task_manager=task_manager,
            reply_store=self._reply_store,
        )
        self._steering_classifier = SteeringClassifier()

    # ── Обработка сообщений ───────────────────────────────────────────────

    async def handle_text_message(self, message: Message) -> ResolvedContext:
        """Обработать обычное текстовое сообщение.

        Args:
            message: Сообщение Telegram (Message).

        Returns:
            ResolvedContext с определённым интентом и привязкой к задаче.
        """
        text = message.text or ""
        chat_id = message.chat.id
        user_id = getattr(message.from_user, "id", 0)
        message_id = message.message_id
        reply_to = message.reply_to_message.message_id if message.reply_to_message else None

        ctx = await self.resolver.resolve(
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            reply_to_message_id=reply_to,
        )

        logger.debug(
            "ContextAdapter: сообщение %d (conv=%d) → %s (task=%s, conf=%.2f)",
            message_id,
            chat_id,
            ctx.intent.value,
            ctx.task_id[:8] if ctx.task_id else "None",
            ctx.confidence,
        )

        return ctx

    async def handle_reply(self, message: Message) -> ResolvedContext:
        """Обработать ответ (Reply) на сообщение со статусом задачи.

        Args:
            message: Сообщение Telegram с reply_to_message_id.

        Returns:
            ResolvedContext с привязкой к задаче по reply.
        """
        return await self.handle_text_message(message)

    async def handle_edit(self, edited_message: Message) -> ResolvedContext:
        """Обработать редактирование сообщения.

        Args:
            edited_message: Отредактированное сообщение Telegram.

        Returns:
            ResolvedContext с IntentType.CORRECT_MESSAGE.
        """
        text = edited_message.text or ""
        chat_id = edited_message.chat.id
        user_id = getattr(edited_message.from_user, "id", 0)
        message_id = edited_message.message_id  # В Telegram ID не меняется при edit

        ctx = await self.resolver.resolve(
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            edited_message_id=message_id,  # ID исходного сообщения
        )

        # Публикуем событие MESSAGE_EDITED
        if self.event_bus is not None and ctx.task_id is not None:
            try:
                await self.event_bus.publish(
                    event_type="MESSAGE_EDITED",
                    task_id=ctx.task_id,
                    conversation_id=chat_id,
                    payload={
                        "message_id": message_id,
                        "revision": ctx.revision,
                        "new_text": text[:500],
                    },
                    source="telegram",
                )
            except Exception as exc:
                logger.warning("Ошибка публикации MESSAGE_EDITED: %s", exc)

        logger.info(
            "ContextAdapter: EDIT сообщения %d (conv=%d) → %s "
            "(task=%s, rev=%d)",
            message_id,
            chat_id,
            ctx.intent.value,
            ctx.task_id[:8] if ctx.task_id else "None",
            ctx.revision,
        )

        return ctx

    async def handle_steering(
        self,
        message: Message,
        task_id: str,
    ) -> None:
        """Направить steering-сигнал в очередь задачи.

        Args:
            message: Сообщение Telegram.
            task_id: UUID задачи.
        """
        text = message.text or ""
        chat_id = message.chat.id

        cmd = await self._steering_queue.push(
            chat_id=chat_id,
            task_id=task_id,
            message=text,
        )

        logger.debug(
            "ContextAdapter: steering [%s] для задачи %s (conv=%d)",
            cmd.signal.value,
            task_id[:8],
            chat_id,
        )

    # ── Маршрутизация ─────────────────────────────────────────────────────

    async def route_to_task_runner(
        self,
        ctx: ResolvedContext,
        message: Message,
    ) -> None:
        """Направить контекст в TaskRunner для создания/исполнения задачи.

        Args:
            ctx: Разрешённый контекст.
            message: Исходное сообщение Telegram (для ответа).
        """
        # Публикуем событие создания задачи
        if self.event_bus is not None:
            try:
                await self.event_bus.publish(
                    event_type="TASK_CREATED",
                    task_id=ctx.task_id or "",
                    conversation_id=ctx.conversation_id,
                    payload={
                        "intent": ctx.intent.value,
                        "text": ctx.steered_text[:200],
                        "message_id": ctx.original_message_id,
                    },
                    source="context_adapter",
                )
            except Exception as exc:
                logger.warning("Ошибка публикации TASK_CREATED: %s", exc)

        # TODO: вызов TaskRunner.run(ctx, message) — будет реализован
        # в Фазе 2 (TaskRunner)

    async def route_status_query(
        self,
        ctx: ResolvedContext,
        message: Message,
    ) -> None:
        """Ответить на запрос статуса задачи.

        Args:
            ctx: Разрешённый контекст с задачей.
            message: Исходное сообщение Telegram.
        """
        if ctx.matched_task is not None:
            task = ctx.matched_task

            progress_bar = self.task_manager.render_progress(task)
            status_text = (
                f"📋 <b>{task.title[:60]}</b>\n"
                f"Статус: <b>{task.status.value}</b>\n"
                f"Прогресс: [{progress_bar}] {task.progress}%\n"
                f"Шагов: {len(task.steps)}\n"
                f"Задача: <code>{task.task_id[:12]}...</code>"
            )
            try:
                await message.answer(
                    status_text,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            except Exception as exc:
                logger.warning("Ошибка отправки статуса: %s", exc)
        else:
            # Нет активных задач — показываем общий статус
            try:
                await message.answer(
                    "📊 Нет активных задач.\n"
                    "Отправьте запрос, чтобы создать новую.",
                )
            except Exception as exc:
                logger.warning("Ошибка отправки статуса: %s", exc)

    async def route_steering(
        self,
        ctx: ResolvedContext,
        message: Message,
    ) -> None:
        """Направить steering-команду в задачу.

        Args:
            ctx: Разрешённый контекст с задачей.
            message: Исходное сообщение Telegram.
        """
        if ctx.task_id is None:
            logger.debug("route_steering: нет task_id, игнорируем")
            return

        # 1. Обновляем current_request задачи через GatewayClient.steer()
        if ctx.steered_text:
            try:
                steer_cmd = SteeringCommand(
                    flow_id=ctx.task_id,
                    command="modify",
                    modification_text=ctx.steered_text,
                )
                await self.gateway_client.steer(ctx.task_id, steer_cmd)
            except Exception as exc:
                logger.warning(
                    "Gateway steer failed for %s: %s",
                    ctx.task_id[:8], exc,
                )
                # Fallback: update local TaskManager
                await self.task_manager.steer_task(ctx.task_id, ctx.steered_text)

        # 2. Кладём в SteeringQueue
        await self.handle_steering(message, ctx.task_id)

        # 3. Подтверждение пользователю
        signal = SteeringClassifier.get_signal(message.text or "")
        signal_icon = {
            SteeringSignal.CONTINUE: "✅",
            SteeringSignal.STOP: "🛑",
            SteeringSignal.MODIFY: "🔄",
            SteeringSignal.PAUSE: "⏸",
            SteeringSignal.RESUME: "▶️",
            SteeringSignal.CANCEL: "🚫",
            SteeringSignal.RETRY: "🔁",
            SteeringSignal.STATUS: "📊",
        }.get(signal, "ℹ️")

        try:
            await message.answer(
                f"{signal_icon} Команда принята: задача будет скорректирована.",
            )
        except Exception as exc:
            logger.warning("Ошибка подтверждения steering: %s", exc)

    async def route_control(
        self,
        ctx: ResolvedContext,
        message: Message,
    ) -> None:
        """Обработать команду управления задачей (cancel/pause/resume/retry).

        Args:
            ctx: Разрешённый контекст с задачей.
            message: Исходное сообщение Telegram.
        """
        if ctx.task_id is None:
            # Нет задачи для управления — игнорируем
            icons = {
                IntentType.CANCEL: "🚫",
                IntentType.PAUSE: "⏸",
                IntentType.RESUME: "▶️",
                IntentType.RETRY: "🔁",
            }
            icon = icons.get(ctx.intent, "ℹ️")
            try:
                await message.answer(
                    f"{icon} Нет активной задачи для этого действия.",
                )
            except Exception as exc:
                logger.warning("Ошибка ответа (нет задачи): %s", exc)
            return

        task_id = ctx.task_id
        result: Any = None

        if ctx.intent == IntentType.CANCEL:
            # Cancel via GatewayClient
            try:
                await self.gateway_client.cancel(
                    flow_id=task_id,
                    reason="user cancel command",
                )
                result = True
            except Exception as exc:
                logger.warning(
                    "Gateway cancel failed for %s: %s",
                    task_id[:8], exc,
                )
                # Fallback to local cancel
                result = await self.task_manager.cancel_task(task_id)
        elif ctx.intent == IntentType.PAUSE:
            result = await self.task_manager.pause_task(task_id)
        elif ctx.intent == IntentType.RESUME:
            result = await self.task_manager.resume_task(task_id)
        elif ctx.intent == IntentType.RETRY:
            result = await self.task_manager.retry_task(task_id)

        if result is not None:
            icons = {
                IntentType.CANCEL: "🚫 Задача отменена.",
                IntentType.PAUSE: "⏸ Задача приостановлена.",
                IntentType.RESUME: "▶️ Задача возобновлена.",
                IntentType.RETRY: "🔄 Повтор задачи...",
            }
            msg = icons.get(ctx.intent, "✅ Команда выполнена.")
            try:
                await message.answer(msg)
            except Exception as exc:
                logger.warning("Ошибка ответа после control: %s", exc)
        else:
            try:
                await message.answer(
                    "❌ Задача не найдена или не может быть изменена.",
                )
            except Exception as exc:
                logger.warning("Ошибка ответа (control failed): %s", exc)

    async def route_edit(
        self,
        ctx: ResolvedContext,
        message: Message,
    ) -> None:
        """Обработать редактирование сообщения задачи.

        Args:
            ctx: Разрешённый контекст с CORRECT_MESSAGE.
            message: Отредактированное сообщение Telegram.
        """
        if ctx.task_id is None:
            logger.debug("route_edit: нет task_id, игнорируем")
            return

        # Обновляем current_request задачи через GatewayClient.steer()
        if ctx.steered_text:
            try:
                steer_cmd = SteeringCommand(
                    flow_id=ctx.task_id,
                    command="modify",
                    modification_text=ctx.steered_text,
                )
                await self.gateway_client.steer(ctx.task_id, steer_cmd)
            except Exception as exc:
                logger.warning(
                    "Gateway steer failed for %s: %s",
                    ctx.task_id[:8], exc,
                )
                # Fallback: update local TaskManager
                await self.task_manager.steer_task(ctx.task_id, ctx.steered_text)

        # Подтверждение
        try:
            await message.answer(
                f"✏️ Сообщение исправлено (ревизия {ctx.revision}). "
                f"Задача обновлена.",
            )
        except Exception as exc:
            logger.warning("Ошибка ответа после edit: %s", exc)

    # ── Привязка сообщений ────────────────────────────────────────────────

    async def bind_status_message(
        self,
        chat_id: int,
        telegram_message_id: int,
        task_id: str,
        step_id: str = "",
        message_type: str = "status",
    ) -> None:
        """Сохранить привязку Telegram-сообщения к задаче.

        Должен вызываться после отправки каждого статус-сообщения.

        Args:
            chat_id: Telegram chat ID.
            telegram_message_id: ID отправленного сообщения.
            task_id: UUID задачи.
            step_id: UUID шага (опционально).
            message_type: Тип сообщения (status, result, question).
        """
        self._reply_store.store_binding(
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            task_id=task_id,
            step_id=step_id,
            message_type=message_type,
        )

        # Также обновляем TaskManager
        await self.task_manager.bind_status_message(
            task_id=task_id,
            message_id=telegram_message_id,
        )

    # ── Свойства ──────────────────────────────────────────────────────────

    @property
    def reply_store(self) -> ReplyMappingStore:
        return self._reply_store

    @property
    def steering_queue(self) -> SteeringQueue:
        return self._steering_queue
