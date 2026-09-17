"""Correlation-id plumbing and structured JSON logging for the gateway."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..observability import event

CORRELATION_HEADER = "X-Correlation-Id"


def ensure_correlation_id(raw: str | None) -> str:
    """Return the inbound correlation id or mint a new UUID4."""
    value = (raw or "").strip()
    return value if value else str(uuid.uuid4())


def log_event(name: str, correlation_id: str, **fields: object) -> None:
    """Structured JSON log line that always carries the correlation id."""
    event(name, service="gateway", correlation_id=correlation_id, **fields)

@dataclass
class CorrelationEnvelope:
    """Stable correlation context for one inbound update."""
    chat_id: int
    telegram_message_id: int
    message_thread_id: int | None
    reply_to_message_id: int | None
    update_id: int
    user_id: int
    account_id: int | None
    source_channel: str
    session_id: str
    operation_id: str
    correlation_id: str
    delivery_key: str
    attempt: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "telegram_message_id": self.telegram_message_id,
            "message_thread_id": self.message_thread_id,
            "reply_to_message_id": self.reply_to_message_id,
            "update_id": self.update_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "source_channel": self.source_channel,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "correlation_id": self.correlation_id,
            "delivery_key": self.delivery_key,
            "attempt": self.attempt,
            "created_at": self.created_at.isoformat(),
        }
