"""Модели событийной системы Antigona.

Task, TaskStep, TaskStatus, TaskEvent — фундамент для
отслеживания жизненного цикла задач и публикации событий.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# ── TaskStatus ─────────────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    """Полный жизненный цикл задачи."""

    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_CONFIRMATION = "waiting_confirmation"
    PAUSED = "paused"
    RETRYING = "retrying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DONE = "done"

    @property
    def is_terminal(self) -> bool:
        """Терминальные статусы — задача завершена."""
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        """Активные статусы — задача в работе."""
        return self in (
            TaskStatus.QUEUED,
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.RETRYING,
        )

    @property
    def is_waiting(self) -> bool:
        """Ожидание внешнего действия."""
        return self in (
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.PAUSED,
        )


# ── TaskStep ────────────────────────────────────────────────────────────────


@dataclass
class TaskStep:
    """Один шаг в рамках задачи.

    Атрибуты:
        step_id: Уникальный UUID шага.
        task_id: ID родительской задачи.
        action_type: Тип действия (WRITE_FILE, RUN_SHELL, RUN_CODE и т.д.).
        action_data: Исходные данные действия (для парсинга).
        path: Путь к файлу (если применимо).
        content: Содержимое (код, текст — если применимо).
        command: Команда оболочки (RUN_SHELL).
        status: Статус выполнения шага.
        progress: Прогресс 0–100.
        stdout: Вывод stdout (обрезанный).
        stderr: Вывод stderr (обрезанный).
        exit_code: Код возврата.
        retry_count: Текущее число попыток.
        max_retries: Максимум попыток (по умолчанию 3).
        started_at: ISO-метка старта.
        finished_at: ISO-метка завершения.
        error: Сообщение об ошибке.
    """

    step_id: str = ""
    task_id: str = ""
    action_type: str = ""
    action_data: str = ""
    path: str | None = None
    content: str | None = None
    command: str | None = None
    status: str = "pending"
    progress: int = 0
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    retry_count: int = 0
    max_retries: int = 3
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.step_id:
            self.step_id = uuid_module.uuid4().hex

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "task_id": self.task_id,
            "action_type": self.action_type,
            "action_data": self.action_data,
            "path": self.path,
            "content": self.content,
            "command": self.command,
            "status": self.status,
            "progress": self.progress,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskStep:
        return cls(**data)


# ── Task ────────────────────────────────────────────────────────────────────


@dataclass
class Task:
    """Полная задача с жизненным циклом.

    Атрибуты:
        task_id: Уникальный UUID задачи.
        conversation_id: Telegram chat ID.
        owner_user_id: ID пользователя-владельца.
        title: Человекочитаемый заголовок.
        original_request: Исходный запрос пользователя.
        current_request: Текущий (возможно уточнённый) запрос.
        status: Текущий статус.
        risk_level: Уровень риска (LOW/MEDIUM/HIGH/CRITICAL).
        priority: Приоритет 0–10.
        created_at: ISO-метка создания.
        updated_at: ISO-метка последнего изменения.
        started_at: ISO-метка первого старта.
        finished_at: ISO-метка завершения.
        current_step_id: ID текущего шага.
        progress: Общий прогресс 0–100.
        telegram_status_message_id: ID одного сообщения со статусом.
        plan: Текст плана (LLM).
        result: Результат выполнения.
        error: Сообщение об ошибке.
        metadata: Произвольные метаданные (dict).
        steps: Список шагов (in-memory).
    """

    task_id: str = ""
    conversation_id: int = 0
    owner_user_id: int = 0
    title: str = ""
    original_request: str = ""
    current_request: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    risk_level: str = "LOW"
    priority: int = 5
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    current_step_id: str | None = None
    progress: int = 0
    telegram_status_message_id: int | None = None
    plan: str = ""
    result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: list[TaskStep] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = uuid_module.uuid4().hex
        now = datetime.now(UTC).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        # Конвертируем статус из строки в enum (для загрузки из JSON)
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        # Преобразуем словари в TaskStep если пришли из JSON
        processed_steps: list[TaskStep] = []
        for s in self.steps:
            if isinstance(s, dict):
                processed_steps.append(TaskStep.from_dict(s))
            elif isinstance(s, TaskStep):
                processed_steps.append(s)
        self.steps = processed_steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "owner_user_id": self.owner_user_id,
            "title": self.title,
            "original_request": self.original_request,
            "current_request": self.current_request,
            "status": self.status.value,
            "risk_level": self.risk_level,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_step_id": self.current_step_id,
            "progress": self.progress,
            "telegram_status_message_id": self.telegram_status_message_id,
            "plan": self.plan,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(**data)


# ── TaskEvent ───────────────────────────────────────────────────────────────


# Типы событий по спецификации §2.1
TASK_EVENT_TYPES = {
    "TASK_CREATED",
    "TASK_QUEUED",
    "TASK_STARTED",
    "PLAN_CREATED",
    "STEP_STARTED",
    "STEP_PROGRESS",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_RETRYING",
    "TASK_PAUSED",
    "TASK_RESUMED",
    "TASK_CANCELLED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "USER_INPUT_REQUIRED",
    "CONFIRMATION_REQUIRED",
    "OWNER_VERIFICATION_REQUIRED",
    "OWNER_VERIFIED",
    "TASK_STEERED",
    "TASK_UPDATED",
    "MESSAGE_EDITED",
    "SECURITY_EVENT",
}


@dataclass
class TaskEvent:
    """Событие жизненного цикла задачи.

    Каждый раз, когда задача меняет состояние, публикуется TaskEvent.
    """

    event_id: str = ""
    event_type: str = ""
    task_id: str = ""
    conversation_id: int = 0
    source: str = "core"
    timestamp: str = ""
    sequence: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = uuid_module.uuid4().hex
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()
        if self.event_type and self.event_type not in TASK_EVENT_TYPES:
            # Разрешаем кастомные типы, но логируем предупреждение
            pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskEvent:
        return cls(**data)


# ── ScheduledAction ─────────────────────────────────────────────────────────


@dataclass
class ScheduledAction:
    """Запланированное действие, которое нужно выполнить после события.

    Используется для отложенных реакций на события.
    """

    action_id: str = ""
    event_type: str = ""
    task_id: str = ""
    action_type: str = ""  # Тип действия (notify, execute_step, и т.д.)
    action_data: dict[str, Any] = field(default_factory=dict)
    delay_seconds: float = 0.0
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.action_id:
            self.action_id = uuid_module.uuid4().hex
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "action_type": self.action_type,
            "action_data": self.action_data,
            "delay_seconds": self.delay_seconds,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledAction:
        return cls(**data)
