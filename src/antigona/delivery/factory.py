from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .adapter import DeliveryAdapter, FakeAdapter, TelegramAdapter
from .adapters import DiscordAdapter, EmailAdapter, SignalAdapter, SlackAdapter, WhatsAppAdapter
from .errors import UnknownChannelError

if TYPE_CHECKING:
    from antigona.config import Settings


AdapterBuilder = Callable[["Settings"], DeliveryAdapter]

_REGISTRY: dict[str, AdapterBuilder] = {}


def register_adapter(name: str, builder: AdapterBuilder) -> None:
    """Register an adapter builder for a channel name."""
    _REGISTRY[name.lower()] = builder


def _build_telegram(settings: Settings) -> DeliveryAdapter:
    return TelegramAdapter(
        bot_token=settings.delivery_telegram_bot_token,
        chat_id=settings.delivery_telegram_chat_id,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_discord(settings: Settings) -> DeliveryAdapter:
    return DiscordAdapter(
        webhook_url=settings.delivery_discord_webhook,
        token=settings.delivery_discord_token,
        channel_id=settings.delivery_discord_channel_id,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_slack(settings: Settings) -> DeliveryAdapter:
    return SlackAdapter(
        webhook_url=settings.delivery_slack_webhook,
        token=settings.delivery_slack_token,
        channel=settings.delivery_slack_channel,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_whatsapp(settings: Settings) -> DeliveryAdapter:
    return WhatsAppAdapter(
        token=settings.delivery_whatsapp_token,
        phone_id=settings.delivery_whatsapp_phone_id,
        to=settings.delivery_whatsapp_to,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_signal(settings: Settings) -> DeliveryAdapter:
    return SignalAdapter(
        url=settings.delivery_signal_url,
        sender=settings.delivery_signal_from,
        recipient=settings.delivery_signal_to,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_email(settings: Settings) -> DeliveryAdapter:
    return EmailAdapter(
        smtp_host=settings.delivery_email_smtp_host,
        smtp_port=settings.delivery_email_smtp_port,
        user=settings.delivery_email_user,
        password=settings.delivery_email_password,
        sender=settings.delivery_email_from,
        recipient=settings.delivery_email_to,
        use_tls=settings.delivery_email_use_tls,
        mock=settings.delivery_mock,
        timeout=settings.delivery_timeout_seconds,
    )


def _build_fake(_settings: Settings) -> DeliveryAdapter:
    return FakeAdapter()


# Default built-in channel registry
register_adapter("telegram", _build_telegram)
register_adapter("discord", _build_discord)
register_adapter("slack", _build_slack)
register_adapter("whatsapp", _build_whatsapp)
register_adapter("signal", _build_signal)
register_adapter("email", _build_email)
register_adapter("fake", _build_fake)


def registered_channels() -> frozenset[str]:
    """Return the immutable set of channel names accepted by the factory."""

    return frozenset(_REGISTRY)


def get_adapter(channel_name: str, settings: Settings) -> DeliveryAdapter:
    """Build and return a DeliveryAdapter for the given channel name."""
    clean_name = (channel_name or "").strip().lower()

    if clean_name == "progress":
        clean_name = (settings.delivery_default_channel or "telegram").strip().lower()

    if clean_name not in _REGISTRY:
        raise UnknownChannelError(f"Unknown delivery channel: '{channel_name}'")

    if (
        settings.delivery_enabled_channels
        and clean_name not in settings.delivery_enabled_channels
        and clean_name != "fake"
    ):
        raise UnknownChannelError(f"Delivery channel '{clean_name}' is disabled in settings")

    builder = _REGISTRY[clean_name]
    return builder(settings)
