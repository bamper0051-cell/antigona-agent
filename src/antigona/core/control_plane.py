"""AntigonaControlPlane — единый application boundary для всех интерфейсов.

Спецификация: мастер-пак §04.2. Все интерфейсы (Telegram, CLI, Dashboard)
проходят через этот протокол. Production реализация вызывает Gateway.
Для тестов допустим FakeControlPlane с тем же контрактом.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# ── Enums ────────────────────────────────────────────────────────────────────


class IntentClass(StrEnum):
    """Классы интентов после IntentRouter (§05.2)."""
    CONVERSATION_ASK = "conversation.ask"
    TASK_CREATE = "task.create"
    TASK_STEER = "task.steer"
    TASK_CORRECT = "task.correct"
    TASK_CANCEL = "task.cancel"
    TASK_PAUSE = "task.pause"
    TASK_RESUME = "task.resume"
    APPROVAL_DECIDE = "approval.decide"
    FLOW_STATUS = "flow.status"
    ARTIFACT_UPLOAD = "artifact.upload"
    SYSTEM_COMMAND = "system.command"


class FlowStatus(StrEnum):
    RECEIVED = "RECEIVED"
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TOOL_EXECUTING = "TOOL_EXECUTING"
    OBSERVING = "OBSERVING"
    WAITING_USER = "WAITING_USER"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    REPLAN_REQUESTED = "REPLAN_REQUESTED"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    POLICY_DENIED = "POLICY_DENIED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


# ── Schemas ──────────────────────────────────────────────────────────────────


@dataclass
class NormalizedRequest:
    """Нормализованный входящий запрос от любого интерфейса (§08)."""
    source: str  # "telegram" | "cli" | "dashboard" | "api"
    correlation_id: str
    conversation_id: str
    owner_id: str | None
    user_message: str
    message_id: int | None = None
    reply_to_message_id: int | None = None
    edited_message_id: int | None = None
    intent: IntentClass | None = None
    intent_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SteeringCommand:
    """Команда управления задачей на лету."""
    flow_id: str
    command: str  # "continue" | "pause" | "resume" | "cancel" | "retry" | "modify"
    modification_text: str | None = None
    correlation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FlowView:
    """Read-only проекция задачи для интерфейсов."""
    flow_id: str
    conversation_id: str
    title: str
    status: FlowStatus
    progress: int
    current_step: str | None
    steps: list[dict[str, Any]]
    events: list[dict[str, Any]]
    result: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class ApprovalView:
    id: str
    tool_name: str
    risk_level: str
    reason: str
    decision: str  # "PENDING" | "APPROVED" | "DENIED"
    decided_by: str | None = None

    @property
    def approval_id(self) -> str:
        return self.id


@dataclass
class ApprovalListEntry:
    id: str
    task_id: str
    tool_name: str
    risk_level: str
    reason: str
    created_at: str

    @property
    def approval_id(self) -> str:
        return self.id

    @property
    def flow_id(self) -> str:
        return self.task_id


@dataclass
class ApprovalListView:
    items: list[ApprovalListEntry]
    total: int


# ── Protocol ─────────────────────────────────────────────────────────────────


class AntigonaControlPlane(Protocol):
    """Единый application boundary для всех интерфейсов.

    В production реализован через GatewayClient.
    В тестах — FakeControlPlane.
    """

    async def submit(self, request: NormalizedRequest) -> FlowView:
        """Создать новую задачу из нормализованного запроса."""
        ...

    async def steer(self, flow_id: str, command: SteeringCommand) -> FlowView:
        """Управление задачей на лету."""
        ...

    async def cancel(self, flow_id: str, reason: str) -> FlowView:
        """Отмена задачи."""
        ...

    async def decide_approval(
        self, approval_id: str, approve: bool
    ) -> ApprovalView:
        """Принять/отклонить approval."""
        ...

    async def get_approval(self, approval_id: str) -> ApprovalView:
        """Получить информацию об approval."""
        ...

    async def list_approvals(
        self, status: str = "PENDING", limit: int = 50, offset: int = 0
    ) -> ApprovalListView:
        """Список approvals."""
        ...

    async def get_flow(self, flow_id: str) -> FlowView:
        """Получить состояние задачи."""
        ...

    async def list_flows(
        self,
        conversation_id: str = "",
        status: FlowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FlowView]:
        """Список задач диалога."""
        ...

    async def stream_events(
        self, conversation_id: str, cursor: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        """Стриминг событий задачи (SSE)."""
        ...
        if False:
            yield {}  # pragma: no cover

