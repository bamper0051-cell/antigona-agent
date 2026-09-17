"""ContextResolver — разрешение контекста сообщения в задачу.

Определяет намерение пользователя (intent) и связывает сообщение
с существующей задачей через Reply, Edit, явный ID или семантику.

Мастер-промпт §§6-9: ContextResolver, Telegram Reply, Editing, Steering.

Архитектура:
  IntentType     — enum всех возможных интентов
  ResolvedContext — dataclass, результат resolve()
  ContextResolver — основной класс с логикой приоритетного разрешения
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from antigona.core import paths
from antigona.tasks.manager import TaskManager
from antigona.tasks.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyContext:
    """Context describing a reply-to-task reference (DB-bound reply).

    Populated when a user replies to an assistant message that is bound to a
    task. Carried in ``ResolvedContext.metadata['reply_context']``.
    """

    reply_to_message_id: int
    target_task_id: str
    target_message_role: str | None = None
    target_original_text: str | None = None


# ── IntentType ─────────────────────────────────────────────────────────────


class IntentType(StrEnum):
    """Намерение пользователя, извлечённое из сообщения.

    Определяет, что система должна сделать с сообщением:
    создать новую задачу, дополнить существующую, изменить её и т.д.
    """

    NEW_TASK = "NEW_TASK"
    """Новый запрос, не связанный с существующей задачей."""

    STEER_EXISTING = "STEER_EXISTING"
    """Корректировка/уточнение существующей задачи ('не используй X',
    'добавь Y')."""

    CORRECT_MESSAGE = "CORRECT_MESSAGE"
    """Редактирование (edit) ранее отправленного сообщения — обновить
    задачу, не создавая новую."""

    ADD_REQUIREMENT = "ADD_REQUIREMENT"
    """Добавление нового требования к существующей задаче ('и ещё ...')."""

    STATUS_QUERY = "STATUS_QUERY"
    """Запрос статуса: 'как там?', 'на каком этапе?', 'что сделано?'."""

    ANSWER = "ANSWER"
    """Ответ на вопрос от агента (ожидание USER_INPUT)."""

    CONFIRM = "CONFIRM"
    """Подтверждение: 'да', 'продолжай', 'начинай', 'ок'."""

    PAUSE = "PAUSE"
    """Приостановка задачи."""

    RESUME = "RESUME"
    """Возобновление задачи."""

    CANCEL = "CANCEL"
    """Отмена задачи."""

    RETRY = "RETRY"
    """Повтор задачи после ошибки."""


# ── ResolvedContext ────────────────────────────────────────────────────────


@dataclass
class ResolvedContext:
    """Результат разрешения контекста сообщения.

    Содержит всю информацию, необходимую TaskRunner или ConversationEngine
    для обработки сообщения: интент, привязанную задачу, метаданные.
    """

    intent: IntentType
    """Определённое намерение пользователя."""

    task_id: str | None
    """ID задачи, к которой относится сообщение (если найдена)."""

    conversation_id: int
    """Telegram chat ID."""

    user_message: str
    """Исходный текст сообщения пользователя."""

    original_message_id: int
    """ID сообщения, которое породило или связано с задачей.
    Для edits — ID исходного сообщения."""

    response_to_message_id: int | None
    """reply_to_message_id (если это ответ на другое сообщение)."""

    revision: int = 0
    """Номер ревизии (для edited_message — инкремент)."""

    steered_text: str = ""
    """Текст запроса после применения steering-коррекции.
    Для NEW_TASK: user_message.
    Для STEER_EXISTING: новый уточнённый запрос.
    Для CORRECT_MESSAGE: исправленный текст.
    """

    matched_task: Task | None = field(default=None, repr=False)
    """Ссылка на найденную задачу (для удобства)."""

    confidence: float = 1.0
    """Уверенность в определении интента (0.0 - 1.0)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Дополнительные метаданные (source, step_id, и т.д.)."""


# ── ReplyMappingStore ──────────────────────────────────────────────────────


# Путь к JSON-файлу для хранения привязки message_id → task_id
_REPLY_MAP_PATH = str(paths.reply_map_file())


class ReplyMappingStore:
    """Хранилище привязок telegram_message_id → task_id.

    Формат файла::

        {
          "chat_OWNER_CHAT_ID": {
            "telegram_msg_12345": {
              "task_id": "uuid",
              "step_id": "uuid",
              "message_type": "status | result | question",
              "created_at": "ISO-8601"
            },
            ...
          },
          ...
        }

    Каждый раз, когда TaskRunner отправляет статус-сообщение в Telegram,
    вызывающий код должен сохранить привязку через ``store_binding()``.
    """

    def __init__(self, path: str = _REPLY_MAP_PATH) -> None:
        self._path = path
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    # ── Public API ─────────────────────────────────────────────────────────

    def store_binding(
        self,
        chat_id: int,
        telegram_message_id: int,
        task_id: str,
        step_id: str = "",
        message_type: str = "status",
    ) -> None:
        """Сохранить привязку Telegram-сообщения к задаче.

        Args:
            chat_id: Telegram chat ID.
            telegram_message_id: ID сообщения в Telegram.
            task_id: UUID задачи.
            step_id: UUID шага (опционально).
            message_type: Тип сообщения (status, result, question).
        """
        chat_key = f"chat_{chat_id}"
        msg_key = f"telegram_msg_{telegram_message_id}"

        from datetime import UTC, datetime

        self._data.setdefault(chat_key, {})[msg_key] = {
            "task_id": task_id,
            "step_id": step_id,
            "message_type": message_type,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._save()

    def resolve_message(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> dict[str, Any] | None:
        """Найти привязку по chat_id + message_id.

        Args:
            chat_id: Telegram chat ID.
            telegram_message_id: ID сообщения в Telegram.

        Returns:
            Словарь с данными привязки или None.
        """
        chat_key = f"chat_{chat_id}"
        msg_key = f"telegram_msg_{telegram_message_id}"
        return self._data.get(chat_key, {}).get(msg_key)

    def remove_binding(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> None:
        """Удалить привязку (например, при удалении сообщения).

        Args:
            chat_id: Telegram chat ID.
            telegram_message_id: ID сообщения в Telegram.
        """
        chat_key = f"chat_{chat_id}"
        msg_key = f"telegram_msg_{telegram_message_id}"
        if chat_key in self._data and msg_key in self._data[chat_key]:
            del self._data[chat_key][msg_key]
            self._save()

    def get_tasks_for_chat(
        self,
        chat_id: int,
    ) -> dict[str, dict[str, Any]]:
        """Получить все привязки для чата.

        Args:
            chat_id: Telegram chat ID.

        Returns:
            Словарь вида {telegram_msg_N: {...}, ...}.
        """
        chat_key = f"chat_{chat_id}"
        return self._data.get(chat_key, {})

    def clear_chat(self, chat_id: int) -> None:
        """Очистить все привязки для чата."""
        chat_key = f"chat_{chat_id}"
        self._data.pop(chat_key, None)
        self._save()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Загрузить привязки из JSON-файла."""
        import json
        from pathlib import Path

        path = Path(self._path)
        if not path.exists():
            self._data = {}
            return
        try:
            raw = path.read_text()
            if raw.strip():
                self._data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Не удалось загрузить %s: %s", self._path, exc)
            self._data = {}

    def _save(self) -> None:
        """Сохранить привязки в JSON-файл."""
        import json
        from pathlib import Path

        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
            )
            path.chmod(0o600)
        except OSError as exc:
            logger.error("Ошибка сохранения %s: %s", self._path, exc)


# ── ContextResolver ────────────────────────────────────────────────────────


class ContextResolver:
    """Разрешает контекст пользовательского сообщения в задачу.

    Алгоритм разрешения (приоритет по спецификации §6):

    1. Reply → найти задачу по reply_to_message_id
    2. Edit  → найти задачу по edited_message_id
    3. Явный task_id в тексте
    4. Единственная активная задача в диалоге
    5. Семантическое совпадение (по тексту запроса)
    6. Новый запрос (NEW_TASK)
    """

    def __init__(
        self,
        task_manager: TaskManager,
        reply_store: ReplyMappingStore | None = None,
        binding_repository: Any | None = None,
    ) -> None:
        self._tm = task_manager
        self._reply_store = reply_store or ReplyMappingStore()
        self._binding_repo = binding_repository

    # ── Основной resolve ───────────────────────────────────────────────────

    async def resolve(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        message_id: int,
        reply_to_message_id: int | None = None,
        edited_message_id: int | None = None,
    ) -> ResolvedContext:
        """Разрешить контекст сообщения.

        Args:
            text: Текст сообщения.
            chat_id: Telegram chat ID.
            user_id: Telegram user ID.
            message_id: ID текущего сообщения.
            reply_to_message_id: ID сообщения, на которое отвечают (или None).
            edited_message_id: Если это edit — ID редактируемого сообщения.

        Returns:
            ResolvedContext с определённым IntentType и привязкой к задаче.
        """
        stripped = text.strip()
        if not stripped:
            return ResolvedContext(
                intent=IntentType.NEW_TASK,
                task_id=None,
                conversation_id=chat_id,
                user_message=text,
                original_message_id=message_id,
                response_to_message_id=reply_to_message_id,
                confidence=0.0,
            )

        # ── Приоритет 1: Reply ────────────────────────────────────────────
        if reply_to_message_id is not None:
            ctx = await self._resolve_by_reply(
                text=stripped,
                chat_id=chat_id,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
            )
            if ctx is not None:
                return ctx

        # ── Приоритет 2: Edit ─────────────────────────────────────────────
        if edited_message_id is not None:
            ctx = await self._resolve_by_edit(
                text=stripped,
                chat_id=chat_id,
                message_id=message_id,
                edited_message_id=edited_message_id,
            )
            if ctx is not None:
                return ctx

        # ── Приоритет 3: Явный task_id в тексте ──────────────────────────
        ctx = await self._resolve_by_explicit_id(stripped, chat_id, message_id)
        if ctx is not None:
            return ctx

        # ── Приоритет 4: Единственная активная задача ─────────────────────
        ctx = await self._resolve_single_active(stripped, chat_id, message_id, reply_to_message_id)
        if ctx is not None:
            return ctx

        # ── Приоритет 5: Семантическое совпадение ─────────────────────────
        ctx = await self._resolve_by_semantic_match(stripped, chat_id, message_id)
        if ctx is not None:
            return ctx

        # ── Приоритет 6: Классифицировать как новый запрос ────────────────
        intent = self._classify_new_intent(stripped)
        return ResolvedContext(
            intent=intent,
            task_id=None,
            conversation_id=chat_id,
            user_message=text,
            original_message_id=message_id,
            response_to_message_id=reply_to_message_id,
            steered_text=stripped,
            confidence=0.7 if intent == IntentType.NEW_TASK else 0.9,
        )

    # ── Приоритет 1: Reply ────────────────────────────────────────────────

    async def _resolve_by_reply(
        self,
        text: str,
        chat_id: int,
        message_id: int,
        reply_to_message_id: int,
    ) -> ResolvedContext | None:
        """Найти задачу по reply_to_message_id.

        Сначала проверяем ReplyMappingStore (новые сообщения со статусом),
        затем TaskManager._by_message (старые привязки через
        bind_status_message).
        """
        # 1a. Пробуем новый ReplyMappingStore
        binding = self._reply_store.resolve_message(chat_id, reply_to_message_id)
        if binding is not None:
            task_id = binding["task_id"]
            task = await self._tm.get_task(task_id)
            if task is not None:
                intent = self._classify_task_intent(text, task)
                return ResolvedContext(
                    intent=intent,
                    task_id=task_id,
                    conversation_id=chat_id,
                    user_message=text,
                    original_message_id=message_id,
                    response_to_message_id=reply_to_message_id,
                    steered_text=self._apply_steering(text, task),
                    matched_task=task,
                    metadata={
                        "source": "reply_new",
                        "step_id": binding.get("step_id", ""),
                        "message_type": binding.get("message_type", "status"),
                    },
                )

        # 1b. Пробуем TaskManager (старый _by_message индекс)
        task = await self._tm.find_task_by_message(chat_id, reply_to_message_id)
        if task is not None:
            intent = self._classify_task_intent(text, task)
            return ResolvedContext(
                intent=intent,
                task_id=task.task_id,
                conversation_id=chat_id,
                user_message=text,
                original_message_id=message_id,
                response_to_message_id=reply_to_message_id,
                steered_text=self._apply_steering(text, task),
                matched_task=task,
                metadata={"source": "reply_taskmanager"},
            )

        # 1c. Пробуем базу данных bindings
        if self._binding_repo is not None:
            db_binding = await self._binding_repo.get_by_message_id(chat_id, reply_to_message_id)
            if db_binding is not None and db_binding.task_id:
                task_id = db_binding.task_id
                task = await self._tm.get_task(task_id)
                if task is not None:
                    intent = self._classify_task_intent(text, task)
                    reply_context = ReplyContext(
                        reply_to_message_id=reply_to_message_id,
                        target_task_id=task_id,
                        target_message_role=db_binding.message_role,
                        target_original_text=db_binding.original_text,
                    )
                    return ResolvedContext(
                        intent=intent,
                        task_id=task_id,
                        conversation_id=chat_id,
                        user_message=text,
                        original_message_id=message_id,
                        response_to_message_id=reply_to_message_id,
                        steered_text=self._apply_steering(text, task),
                        matched_task=task,
                        metadata={
                            "source": "reply_binding_db",
                            "reply_context": reply_context,
                        },
                    )

        return None

    # ── Приоритет 2: Edit ─────────────────────────────────────────────────

    async def _resolve_by_edit(
        self,
        text: str,
        chat_id: int,
        message_id: int,
        edited_message_id: int,
    ) -> ResolvedContext | None:
        """Найти задачу по edited_message_id.

        Отредактированное сообщение — это исправление предыдущего запроса.
        Если найдена задача — возвращаем CORRECT_MESSAGE.
        """
        # 1. Проверяем базу данных bindings
        if self._binding_repo is not None:
            db_binding = await self._binding_repo.get_by_message_id(chat_id, edited_message_id)
            if db_binding is not None and db_binding.task_id:
                task_id = db_binding.task_id
                task = await self._tm.get_task(task_id)
                if task is not None:
                    revision = task.metadata.get("edit_revision", 0) + 1
                    await self._tm.update_task(
                        task_id=task_id,
                        metadata={**task.metadata, "edit_revision": revision},
                    )
                    was_text = db_binding.edited_text or db_binding.original_text or ""
                    steered_text = f"Пользователь исправил запрос: было «{was_text}» стало «{text}»"
                    return ResolvedContext(
                        intent=IntentType.CORRECT_MESSAGE,
                        task_id=task_id,
                        conversation_id=chat_id,
                        user_message=text,
                        original_message_id=edited_message_id,
                        response_to_message_id=None,
                        revision=revision,
                        steered_text=steered_text,
                        matched_task=task,
                        metadata={"source": "edit_binding_db"},
                    )

        # 2. Пробуем ReplyMappingStore
        binding = self._reply_store.resolve_message(chat_id, edited_message_id)
        if binding is not None:
            task_id = binding["task_id"]
            task = await self._tm.get_task(task_id)
            if task is not None:
                # Находим номер ревизии из метаданных задачи
                revision = task.metadata.get("edit_revision", 0) + 1
                await self._tm.update_task(
                    task_id,
                    metadata={**task.metadata, "edit_revision": revision},
                )
                return ResolvedContext(
                    intent=IntentType.CORRECT_MESSAGE,
                    task_id=task_id,
                    conversation_id=chat_id,
                    user_message=text,
                    original_message_id=edited_message_id,
                    response_to_message_id=None,
                    revision=revision,
                    steered_text=text,
                    matched_task=task,
                    metadata={"source": "edit"},
                )

        # 3. Пробуем TaskManager
        task = await self._tm.find_task_by_message(chat_id, edited_message_id)
        if task is not None:
            revision = task.metadata.get("edit_revision", 0) + 1
            await self._tm.update_task(
                task_id=task.task_id,
                metadata={**task.metadata, "edit_revision": revision},
            )
            return ResolvedContext(
                intent=IntentType.CORRECT_MESSAGE,
                task_id=task.task_id,
                conversation_id=chat_id,
                user_message=text,
                original_message_id=edited_message_id,
                response_to_message_id=None,
                revision=revision,
                steered_text=text,
                matched_task=task,
                metadata={"source": "edit"},
            )

        return None

    # ── Приоритет 3: Явный task_id в тексте ──────────────────────────────

    async def _resolve_by_explicit_id(
        self,
        text: str,
        chat_id: int,
        message_id: int,
    ) -> ResolvedContext | None:
        """Проверить, содержит ли текст явный task_id (UUID hex)."""
        # Ищем 32-символьный hex (формат uuid4.hex)
        match = re.search(r"\b([a-f0-9]{32})\b", text.lower())
        if not match:
            return None

        task_id = match.group(1)
        task = await self._tm.get_task(task_id)
        if task is None:
            return None

        # Определяем интент: если остальной текст — команда управления
        remaining = text.replace(match.group(0), "").strip()
        intent = self._classify_task_intent(remaining or "статус", task)

        return ResolvedContext(
            intent=intent,
            task_id=task_id,
            conversation_id=chat_id,
            user_message=text,
            original_message_id=message_id,
            response_to_message_id=None,
            steered_text=self._apply_steering(remaining, task),
            matched_task=task,
            metadata={"source": "explicit_id"},
        )

    # ── Приоритет 4: Единственная активная задача ─────────────────────────

    async def _resolve_single_active(
        self,
        text: str,
        chat_id: int,
        message_id: int,
        reply_to_message_id: int | None,
    ) -> ResolvedContext | None:
        """Если в диалоге ровно одна активная задача — привязываем к ней."""
        active = await self._tm.get_active_tasks(chat_id)
        if len(active) != 1:
            return None

        task = active[0]
        intent = self._classify_task_intent(text, task)

        return ResolvedContext(
            intent=intent,
            task_id=task.task_id,
            conversation_id=chat_id,
            user_message=text,
            original_message_id=message_id,
            response_to_message_id=reply_to_message_id,
            steered_text=self._apply_steering(text, task),
            matched_task=task,
            confidence=0.8,
            metadata={"source": "single_active"},
        )

    # ── Приоритет 5: Семантическое совпадение ────────────────────────────

    async def _resolve_by_semantic_match(
        self,
        text: str,
        chat_id: int,
        message_id: int,
    ) -> ResolvedContext | None:
        """Поиск активных задач по текстовому совпадению.

        Использует встроенный `get_active_tasks_by_request` TaskManager.
        """
        matches = await self._tm.get_active_tasks_by_request(text)
        if not matches:
            return None

        # Берём наиболее релевантную (первую)
        task = matches[0]
        intent = self._classify_task_intent(text, task)

        return ResolvedContext(
            intent=intent,
            task_id=task.task_id,
            conversation_id=chat_id,
            user_message=text,
            original_message_id=message_id,
            response_to_message_id=None,
            steered_text=self._apply_steering(text, task),
            matched_task=task,
            confidence=0.65,
            metadata={"source": "semantic_match", "match_count": len(matches)},
        )

    # ── Классификация интентов ───────────────────────────────────────────

    @staticmethod
    def _classify_task_intent(text: str, task: Task | None = None) -> IntentType:
        """Классифицировать намерение для сообщения, привязанного к задаче.

        Использует SteeringClassifier для распознавания команд управления,
        затем проверяет статус-запросы и steering.

        Приоритет: команды управления → статус → steering → продолжение.
        """
        from antigona.tasks.steering import SteeringClassifier, SteeringSignal

        lower = text.lower().strip()

        # ── Шаг 1: SteeringClassifier (точное распознавание команд) ──────
        sc = SteeringClassifier()
        cmd = sc.classify(text)

        # Cancel
        if cmd.signal == SteeringSignal.CANCEL:
            return IntentType.CANCEL

        # Pause
        if cmd.signal == SteeringSignal.PAUSE:
            return IntentType.PAUSE

        # Resume
        if cmd.signal == SteeringSignal.RESUME:
            return IntentType.RESUME

        # Retry
        if cmd.signal == SteeringSignal.RETRY:
            return IntentType.RETRY

        # Continue / подтверждение
        if cmd.signal == SteeringSignal.CONTINUE:
            return IntentType.CONFIRM

        # Статус
        if cmd.signal == SteeringSignal.STATUS:
            return IntentType.STATUS_QUERY

        # Stop — тоже CANCEL
        if cmd.signal == SteeringSignal.STOP:
            return IntentType.CANCEL

        # ── Шаг 2: Если задача ждёт ответа от пользователя — ANSWER ─────
        if task is not None and task.status.value in (
            "waiting_user", "waiting_confirmation",
        ):
            return IntentType.ANSWER

        # ── Шаг 3: Добавление требования ────────────────────────────────
        if lower.startswith(("и ещё", "ещё", "также", "добавь", "добавить",
                              "plus", "++", "и ещё нужно", "а ещё")):
            return IntentType.ADD_REQUIREMENT

        # ── Шаг 4: Всё остальное — Steering (уточнение текущей задачи) ──
        if cmd.signal == SteeringSignal.MODIFY or cmd.confidence < 0.6:
            return IntentType.STEER_EXISTING

        return IntentType.STEER_EXISTING

    @staticmethod
    def _classify_new_intent(text: str) -> IntentType:
        """Классифицировать намерение для нового (непривязанного) сообщения.

        Без контекста задачи определяем: статус-запрос, управление (которое
        не к чему привязать), или новая задача.
        """
        lower = text.lower().strip()

        # Статус-запрос без задачи
        if ContextResolver._is_status_query(lower):
            return IntentType.STATUS_QUERY

        # Cancel/Pause/Resume без задачи — всё равно CANCEL (ничего не делаем)
        if lower in ("отмени", "отмена", "cancel", "стоп"):
            return IntentType.CANCEL

        # Всё остальное — новый запрос
        return IntentType.NEW_TASK

    # ── Хелперы классификации ─────────────────────────────────────────────

    @staticmethod
    def _is_status_query(text: str) -> bool:
        """Проверить, является ли текст запросом статуса."""
        status_patterns = (
            "как там",
            "на каком этап",
            "что сделано",
            "какой статус",
            "статус",
            "прогресс",
            "progress",
            "что готово",
            "сколько осталось",
            "как дела",
            "чё там",
            "чё по",
            "как успехи",
            "где мы",
            "статус задачи",
            "покажи статус",
            "отчёт",
            "report",
        )
        return any(p in text for p in status_patterns)

    @staticmethod
    def _is_steering_command(text: str) -> bool:
        """Проверить, является ли текст steering-командой."""
        steering_patterns = (
            "не используй",
            "сделай вместо",
            "добавь",
            "убери",
            "измени",
            "замени",
            "вместо",
            "используй другой",
            "попробуй по-другому",
            "сделай иначе",
            "не надо",
            "не нужно",
            "без",
            "только",
            "исправь",
            "обнови",
            "переделай",
            "с другим",
            "поменяй",
        )
        return any(p in text for p in steering_patterns)

    # ── Steering ──────────────────────────────────────────────────────────

    @staticmethod
    def _apply_steering(text: str, task: Task | None = None) -> str:
        """Применить steering: если задача есть, сформировать уточнённый запрос.

        Для REPLY/EDIT: объединяем current_request задачи с новым текстом.
        Для NEW_TASK: возвращаем текст как есть.
        """
        if task is None:
            return text
        # Объединяем: текущий запрос задачи + новое уточнение
        current = task.current_request or task.original_request or ""
        if current and text:
            return f"{current}. Дополнение: {text}"
        return text or current

    # ── Свойства ──────────────────────────────────────────────────────────

    @property
    def reply_store(self) -> ReplyMappingStore:
        """Доступ к хранилищу привязок."""
        return self._reply_store
