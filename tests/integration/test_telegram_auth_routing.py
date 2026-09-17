"""P0 routing defect: exact auth commands must win over the generic F.text handler.

Proves the bug (and later the fix) through the REAL aiogram dispatch path —
``Dispatcher.feed_update(...)`` — never by calling a handler callback directly.
Directly invoking ``router.message.handlers[N].callback(...)`` would bypass
aiogram's first-match-wins routing entirely and hide this exact defect.

Confirmed defect (see docs/ROADMAP.md): the generic
``@router.message(F.text)`` handler is registered before the exact
``Command("unlock")`` / ``Command("lock")`` / ``Command("auth_status")``
handlers, so aiogram's first-match-wins router dispatch sends those commands
into the conversational LLM pipeline instead of the auth control plane.
"""
from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import GetMe, SendChatAction, SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from antigona.channels.telegram.bot import TelegramBot

BOT_USERNAME = "antigona_test_bot"

_update_id_counter = itertools.count(10_000)


def _fake_message(chat_id: int | str, text: str = "") -> Message:
    return Message(
        message_id=next(_update_id_counter),
        date=datetime.now(),
        chat=Chat(id=int(chat_id), type="private"),
        text=text,
    )


def _make_fake_call(sent: list[TelegramMethod[Any]]) -> Any:
    async def fake_call(
        self: Bot, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        sent.append(method)
        if isinstance(method, GetMe):
            return TgUser(
                id=1, is_bot=True, first_name="TestBot", username=BOT_USERNAME
            )
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, SendMessage):
            return _fake_message(method.chat_id, method.text or "")
        return True

    return fake_call


class _RaisingSpy:
    """Callable spy that raises if invoked, and records whether it was."""

    def __init__(self, exc: type[BaseException] = AssertionError) -> None:
        self.called = False
        self._exc = exc

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.called = True
        raise self._exc(
            "control-plane command must not reach the LLM/Gateway path"
        )


def _build_bot(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> TelegramBot:
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)

    db_path = tmp_path / "auth_routing_test.db"
    bot = TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://testserver",
        gateway_token="gateway-token",
        database_url=f"sqlite:///{db_path}",
    )
    return bot


async def _feed_command(bot: TelegramBot, sent: list[TelegramMethod[Any]], text: str) -> str:
    """Feed one text message as a real Telegram update; return the bot's reply text."""
    chat_id = 555111
    user = TgUser(id=999, is_bot=False, first_name="Owner")
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=next(_update_id_counter),
        date=datetime.now(),
        chat=chat,
        from_user=user,
        text=text,
    )
    update = Update(update_id=next(_update_id_counter), message=message)

    before = len(sent)
    await bot.dp.feed_update(bot=bot.bot, update=update)

    replies = [
        m.text
        for m in sent[before:]
        if isinstance(m, SendMessage) and m.text
    ]
    return replies[-1] if replies else ""


AUTH_COMMANDS = [
    ("/auth_status", "Владелец"),
    (f"/auth_status@{BOT_USERNAME}", "Владелец"),
    ("/unlock", "PIN"),
    (f"/unlock@{BOT_USERNAME}", "PIN"),
    ("/lock", "🔒"),
    (f"/lock@{BOT_USERNAME}", "🔒"),
    ("/confirm sometoken123", "одтвержд"),
    (f"/confirm@{BOT_USERNAME} sometoken123", "одтвержд"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("command_text,expected_marker", AUTH_COMMANDS)
async def test_auth_command_hits_exact_handler_not_llm_or_gateway(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    command_text: str,
    expected_marker: str,
) -> None:
    bot = _build_bot(tmp_path, monkeypatch)

    llm_spy = _RaisingSpy()
    gateway_spy = _RaisingSpy()
    monkeypatch.setattr("antigona.channels.telegram.bot.chitchat_reply", llm_spy)
    monkeypatch.setattr(bot.gateway_client, "submit", gateway_spy)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _make_fake_call(sent))

    reply_text = await _feed_command(bot, sent, command_text)

    # (b) control plane must never touch the LLM provider or the Gateway.
    assert not llm_spy.called, (
        f"{command_text!r} reached the conversational LLM provider "
        "(chitchat_reply) instead of the exact auth handler"
    )
    assert not gateway_spy.called, (
        f"{command_text!r} reached the Gateway client instead of the "
        "exact auth handler"
    )

    # (a) the exact auth handler ran — reply is the AUTH response.
    assert expected_marker in reply_text, (
        f"{command_text!r} did not produce the AUTH response "
        f"(expected marker {expected_marker!r} in reply: {reply_text!r})"
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
