from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, User

from antigona.channels.telegram.bot import TelegramBot
from antigona.core.gateway_client import GatewayClient


@pytest.fixture(autouse=True)
async def close_telegram_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Close the bot and its constructor-owned GatewayClient after each probe."""
    instances: list[tuple[TelegramBot, GatewayClient]] = []
    original_init = TelegramBot.__init__

    def tracked_init(self: TelegramBot, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        instances.append((self, self.gateway_client))

    monkeypatch.setattr(TelegramBot, "__init__", tracked_init)
    yield
    for bot, gateway in instances:
        await bot.close()
        await gateway.close()


@pytest.mark.anyio
async def test_edited_message_handler_forwards_to_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 4: edited message — транспорт отправляет правку в ядро (Turn API).

    The editing user (id=456) is the configured owner; R1-TELEGRAM-01 added the
    fail-closed owner gate to ``edited_message_handler`` (a non-owner edit is
    now denied — see tests/integration/test_telegram_owner_gate_all_inputs.py).
    """
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "456")
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://localhost:8090",
        gateway_token="dev-token",
    )

    async def fake_call(self: Any, method: Any, request_timeout: Any = None) -> Any:
        return MagicMock(message_id=999)
    monkeypatch.setattr(Bot, "__call__", fake_call)

    bot.binding_repo = AsyncMock()
    bot.operation_store = AsyncMock()
    bot.event_bus = AsyncMock()

    async def _turn(**kwargs):
        return {
            "reply": "Задача скорректирована.",
            "session_id": "telegram:123",
            "response_type": "control",
            "flow_id": "flow-1",
            "requires_approval": False,
            "verified": None,
        }

    turn_mock = AsyncMock(side_effect=_turn)
    monkeypatch.setattr(bot.gateway_client, "send_dialogue_turn", turn_mock)
    bot.binding_repo.save = AsyncMock()

    chat = Chat(id=123, type="private")
    user = User(id=456, is_bot=False, first_name="Test")
    message = Message(
        message_id=789,
        chat=chat,
        date=datetime.now(),
        text="Исправленный текст",
        from_user=user,
    )

    handlers = bot.router.edited_message.handlers
    assert len(handlers) > 0
    handler = handlers[0].callback

    await handler(message.as_(bot.bot))

    # Правка уходит в ядро; пользователь получает ответ (через Bot.__call__).
    turn_mock.assert_awaited_once()
    sent = message.as_(bot.bot)
    assert sent.answer is not None
