from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.commands import CommandDisposition
from antigona.core.gateway_client import GatewayClient


@pytest.fixture(autouse=True)
async def close_telegram_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Close every TelegramBot and the GatewayClient it constructed."""
    from antigona.channels.telegram.bot import TelegramBot

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


@pytest.mark.asyncio
async def test_gateway_client_send_dialogue_turn() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/dialogue/turn"
        return httpx.Response(
            200,
            json={
                "reply": "Привет! Чем могу помочь?",
                "session_id": "session-1",
                "verified": None,
                "response_type": "conversation",
                "flow_id": None,
                "requires_approval": False,
            },
        )

    transport = httpx.MockTransport(handler)
    client = GatewayClient(base_url="http://127.0.0.1:8765", transport=transport)

    res = await client.send_dialogue_turn(
        text="Привет",
        session_id="session-1",
        channel="cli",
        user_id="default",
    )
    assert res == {
        "reply": "Привет! Чем могу помочь?",
        "session_id": "session-1",
        "verified": None,
        "response_type": "conversation",
        "flow_id": None,
        "requires_approval": False,
    }
    await client.close()


@pytest.mark.asyncio
async def test_chat_controller_uses_gateway_send_dialogue_turn() -> None:
    mock_gateway = MagicMock()
    mock_gateway.send_dialogue_turn = AsyncMock(
        return_value={
            "reply": "Ответ через Gateway!",
            "session_id": "cli-session",
            "verified": None,
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
        }
    )

    controller = ChatController(gateway=mock_gateway, conversation_id="cli-session")
    disposition = await controller.handle_input("Привет!")

    assert disposition == CommandDisposition.LOCAL_ACTION
    mock_gateway.send_dialogue_turn.assert_awaited_once()
    call_args = mock_gateway.send_dialogue_turn.await_args.kwargs
    assert call_args["text"] == "Привет!"
    assert call_args["session_id"] == "cli-session"
    assert call_args["channel"] == "cli"
    assert call_args["user_id"] == "owner"  # canonical CLI principal (LOCAL_TRUSTED_PRINCIPAL)
    assert call_args["turn_id"].startswith("cli:turn:")
    assert any("Ответ через Gateway!" in msg.content for msg in controller.state.messages)


@pytest.mark.asyncio
async def test_telegram_bot_uses_gateway_send_dialogue_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Step 4: text_handler — весь текст уходит в Gateway Turn API."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock as MM

    from aiogram import Bot
    from aiogram.types import Chat, Message, User

    import antigona.channels.telegram.bot as bot_mod

    db_file = str(tmp_path / "test_bot.db")
    real_bot = bot_mod.TelegramBot(
        token="1234567890:" + "ABCdefGHIJklmNOPqrstUVwxyz-1234567890",
        gateway_url="http://localhost:1",
        database_url=f"sqlite:///{db_file}",
    )
    mock_gateway = MM()
    mock_gateway.send_dialogue_turn = AsyncMock(
        return_value={
            "reply": "Telegram ответ от Gateway",
            "session_id": "telegram:12345",
            "verified": None,
            "response_type": "conversation",
            "flow_id": None,
            "requires_approval": False,
        }
    )
    real_bot.gateway_client = mock_gateway
    real_bot.operation_store = AsyncMock()
    real_bot.event_bus = AsyncMock()
    real_bot.binding_repo = AsyncMock()
    real_bot.operation_store.find_active_by_progress_message = AsyncMock(return_value=None)
    real_bot.operation_store.create = AsyncMock(
        return_value=SimpleNamespace(id="op-1")
    )

    async def fake_call(self: object, method: object, request_timeout: object = None) -> MM:
        return MM(message_id=999)
    monkeypatch.setattr(Bot, "__call__", fake_call)

    chat = Chat(id=12345, type="private")
    user = User(id=99999, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        chat=chat,
        date=__import__("datetime").datetime.now(),
        text="Привет бот",
        from_user=user,
    )

    handler = next(
        h.callback for h in real_bot.router.message.handlers
        if h.callback.__name__ == "text_handler"
    )
    # Owner gate reads ANTIGONA_OWNER_ID at call time; pin it to the test user
    # so the transport contract (send_dialogue_turn) is what gets exercised.
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "99999")
    await handler(message.as_(real_bot.bot))

    mock_gateway.send_dialogue_turn.assert_awaited_once()
    call = mock_gateway.send_dialogue_turn.await_args
    assert call.kwargs["text"] == "Привет бот"
    assert call.kwargs["session_id"] == "telegram:12345"
    assert call.kwargs["channel"] == "telegram"
    assert call.kwargs["user_id"] == "99999"
