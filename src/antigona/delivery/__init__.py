from __future__ import annotations

from .adapter import DeliveryAdapter, FakeAdapter, ProgressEvent, TelegramAdapter
from .adapters import DiscordAdapter, EmailAdapter, SignalAdapter, SlackAdapter, WhatsAppAdapter
from .errors import (
    DeliveryConfigError,
    DeliveryError,
    DeliveryPermanentError,
    DeliveryProviderRejected,
    UnknownChannelError,
    sanitize_delivery_error,
)
from .factory import get_adapter, register_adapter
from .readback import (
    READ_BACK_REFUTED,
    READ_BACK_SEND_ACK,
    READ_BACK_STATUSES,
    READ_BACK_UNSUPPORTED,
    DeliveryOutcome,
    read_back_confirms_transmission,
)
from .router import Router
from .worker import DeliveryWorker

__all__ = [
    "ProgressEvent",
    "DeliveryAdapter",
    "TelegramAdapter",
    "FakeAdapter",
    "DiscordAdapter",
    "SlackAdapter",
    "WhatsAppAdapter",
    "SignalAdapter",
    "EmailAdapter",
    "DeliveryError",
    "DeliveryPermanentError",
    "DeliveryConfigError",
    "DeliveryProviderRejected",
    "UnknownChannelError",
    "sanitize_delivery_error",
    "get_adapter",
    "register_adapter",
    "Router",
    "DeliveryWorker",
    "DeliveryOutcome",
    "READ_BACK_SEND_ACK",
    "READ_BACK_UNSUPPORTED",
    "READ_BACK_REFUTED",
    "READ_BACK_STATUSES",
    "read_back_confirms_transmission",
]
