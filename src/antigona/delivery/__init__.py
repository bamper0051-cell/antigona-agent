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
]
