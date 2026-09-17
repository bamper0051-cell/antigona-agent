from __future__ import annotations

from .discord import DiscordAdapter
from .email_adapter import EmailAdapter
from .signal import SignalAdapter
from .slack import SlackAdapter
from .whatsapp import WhatsAppAdapter

__all__ = [
    "DiscordAdapter",
    "SlackAdapter",
    "WhatsAppAdapter",
    "SignalAdapter",
    "EmailAdapter",
]
