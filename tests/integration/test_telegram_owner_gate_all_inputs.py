"""R1-TELEGRAM-01 (T0021 R1-B06) — every Telegram input that can create or
steer a core turn must pass the SAME fail-closed owner gate
(``OwnerIdentity.is_owner``, False when ``ANTIGONA_OWNER_ID`` is unset).

Regression target: ``voice_message_handler`` and ``edited_message_handler`` in
``antigona/channels/telegram/bot.py`` called ``self._bridge_turn(...)`` with no
owner check, so a non-owner could drive the core through voice / message-edit
even though the text, attachment, command and callback handlers all deny.

Exercised through the REAL aiogram dispatch path (``Dispatcher.feed_update``),
not by poking handler callbacks directly.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    GetFile,
    GetMe,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    Chat,
    File,
    Message,
    Update,
    Voice,
)
from aiogram.types import User as TgUser

from antigona.channels.telegram.bot import TelegramBot

BOT_USERNAME = "antigona_test_bot"
OWNER_ID = 4242
STRANGER_ID = 999

_ids = itertools.count(70_000)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _fake_call(sent: list[TelegramMethod[Any]]) -> Any:
    async def call(
        self: Bot, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        sent.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=1, is_bot=True, first_name="TestBot", username=BOT_USERNAME)
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids), date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text or "",
            )
        if isinstance(method, GetFile):
            return File(file_id="f", file_unique_id="fu", file_path="voice/x.ogg")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    return call


def _build_bot(tmp_path: Any) -> TelegramBot:
    return TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://testserver",
        gateway_token="gateway-token",
        database_url=f"sqlite:///{tmp_path / 't0029.db'}",
    )


def _voice_update(user_id: int) -> Update:
    msg = Message(
        message_id=next(_ids), date=datetime.now(),
        chat=Chat(id=555, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="U"),
        voice=Voice(file_id="v1", file_unique_id="vu1", duration=3),
    )
    return Update(update_id=next(_ids), message=msg)


def _edited_update(user_id: int, text: str = "исправленный текст") -> Update:
    msg = Message(
        message_id=next(_ids), date=datetime.now(),
        chat=Chat(id=555, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=next(_ids), edited_message=msg)


def _text_update(user_id: int, text: str = "сделай что-нибудь") -> Update:
    msg = Message(
        message_id=next(_ids), date=datetime.now(),
        chat=Chat(id=555, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=next(_ids), message=msg)


def _denied(sent: list[TelegramMethod[Any]], before: int) -> bool:
    return any(
        isinstance(m, SendMessage) and "🚫" in (m.text or "")
        for m in sent[before:]
    )


@pytest.mark.anyio
async def test_nonowner_voice_is_denied_and_never_reaches_core(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    bot = _build_bot(tmp_path)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _fake_call(sent))

    bridge_spy = _spy()
    monkeypatch.setattr(TelegramBot, "_bridge_turn", bridge_spy)
    monkeypatch.setattr(Bot, "download_file", _async_return(None))
    monkeypatch.setattr(
        "antigona.channels.telegram.bot.speech_to_text",
        _async_return("transcribed"),
    )

    before = len(sent)
    try:
        await bot.dp.feed_update(bot=bot.bot, update=_voice_update(STRANGER_ID))
        assert bridge_spy.calls == 0, "non-owner voice reached _bridge_turn (core)"
        assert _denied(sent, before), "non-owner voice was not denied with 🚫"
    finally:
        await bot.close()
        await bot.gateway_client.close()


@pytest.mark.anyio
async def test_nonowner_edited_message_is_denied_and_never_reaches_core(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    bot = _build_bot(tmp_path)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _fake_call(sent))

    bridge_spy = _spy()
    monkeypatch.setattr(TelegramBot, "_bridge_turn", bridge_spy)

    before = len(sent)
    try:
        await bot.dp.feed_update(bot=bot.bot, update=_edited_update(STRANGER_ID))
        assert bridge_spy.calls == 0, "non-owner edit reached _bridge_turn (core)"
        assert _denied(sent, before), "non-owner edit was not denied with 🚫"
    finally:
        await bot.close()
        await bot.gateway_client.close()


@pytest.mark.anyio
async def test_nonowner_text_is_denied_regression_guard(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text was already gated; lock it in alongside voice/edit."""
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    bot = _build_bot(tmp_path)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _fake_call(sent))
    bridge_spy = _spy()
    monkeypatch.setattr(TelegramBot, "_bridge_turn", bridge_spy)

    before = len(sent)
    try:
        await bot.dp.feed_update(bot=bot.bot, update=_text_update(STRANGER_ID))
        assert bridge_spy.calls == 0
        assert _denied(sent, before)
    finally:
        await bot.close()
        await bot.gateway_client.close()


@pytest.mark.anyio
async def test_owner_voice_reaches_core(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", str(OWNER_ID))
    bot = _build_bot(tmp_path)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _fake_call(sent))
    monkeypatch.setattr(Bot, "download_file", _async_return(None))
    monkeypatch.setattr(
        "antigona.channels.telegram.bot.speech_to_text",
        _async_return("transcribed text"),
    )
    bridge_spy = _spy(reply="ok")
    monkeypatch.setattr(TelegramBot, "_bridge_turn", bridge_spy)

    try:
        await bot.dp.feed_update(bot=bot.bot, update=_voice_update(OWNER_ID))
        assert bridge_spy.calls == 1, "owner voice did not reach _bridge_turn"
        assert bridge_spy.last_kind == "voice"
    finally:
        await bot.close()
        await bot.gateway_client.close()


@pytest.mark.anyio
async def test_owner_edited_message_reaches_core(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", str(OWNER_ID))
    bot = _build_bot(tmp_path)
    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _fake_call(sent))
    bridge_spy = _spy(reply="ok")
    monkeypatch.setattr(TelegramBot, "_bridge_turn", bridge_spy)

    try:
        await bot.dp.feed_update(bot=bot.bot, update=_edited_update(OWNER_ID))
        assert bridge_spy.calls == 1, "owner edit did not reach _bridge_turn"
        assert bridge_spy.last_kind == "edited"
    finally:
        await bot.close()
        await bot.gateway_client.close()


# ── helpers ────────────────────────────────────────────────────────────────


class _BridgeSpy:
    def __init__(self, reply: str = "") -> None:
        self.calls = 0
        self.last_kind: str | None = None
        self._reply = reply

    async def __call__(
        self, message: Any, text: str, *, kind: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_kind = kind
        return {"reply": self._reply, "flow_id": "flow-x"}


def _spy(reply: str = "") -> _BridgeSpy:
    return _BridgeSpy(reply)


def _async_return(value: Any) -> Any:
    async def _f(*_a: Any, **_kw: Any) -> Any:
        return value
    return _f
