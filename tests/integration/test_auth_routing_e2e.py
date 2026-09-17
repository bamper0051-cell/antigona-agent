"""P1 Phase 7: auth routing E2E through the REAL aiogram Dispatcher
(restored from pyc contract after rollback). No-LLM/no-Gateway spies prove
auth commands never touch conversational/Gateway paths."""
from __future__ import annotations

from datetime import datetime
from itertools import count
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import GetMe, SendChatAction, SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, Message, Update, User

from antigona.channels.telegram.bot import TelegramBot

_update_id_counter = count(1000)
BOT_USERNAME = "antigona_test_bot"


def _fake_message(chat_id: int | str, text: str) -> Message:
    return Message(
        message_id=next(_update_id_counter),
        date=datetime.now(),
        chat=Chat(id=int(chat_id), type="private"),
        from_user=User(id=1, is_bot=False, first_name="Test"),
        text=text,
    )


def _make_fake_call(sent: list[TelegramMethod[Any]]):
    async def fake_call(
        self: Bot, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        sent.append(method)
        if isinstance(method, GetMe):
            return User(
                id=1, is_bot=True, first_name="TestBot", username=BOT_USERNAME
            )
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, SendMessage):
            return _fake_message(method.chat_id, method.text or "")
        return True

    return fake_call


class _RaisingSpy:
    def __init__(self, exc: type[BaseException] = AssertionError) -> None:
        self.called = False
        self._exc = exc

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.called = True
        raise self._exc("auth command must not call LLM/Gateway path")


def _build_bot(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> TelegramBot:
    db_path = tmp_path / "auth_phase7_e2e.db"
    return TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://testserver",
        gateway_token="gateway-token",
        database_url=f"sqlite:///{db_path}",
    )


async def _feed_command(
    bot: TelegramBot,
    sent: list[TelegramMethod[Any]],
    text: str,
    *,
    user_id: int,
) -> str:
    chat_id = 955_111
    user = User(id=user_id, is_bot=False, first_name="User")
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

    replies = [m.text for m in sent[before:] if isinstance(m, SendMessage) and m.text]
    return replies[-1] if replies else ""


@pytest.mark.anyio
async def test_non_owner_unlock_blocked_and_no_llm_gateway(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")

    bot = _build_bot(tmp_path, monkeypatch)
    llm_spy = _RaisingSpy()
    gateway_spy = _RaisingSpy()
    monkeypatch.setattr("antigona.channels.telegram.bot.chitchat_reply", llm_spy)
    monkeypatch.setattr(bot.gateway_client, "submit", gateway_spy)

    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _make_fake_call(sent))

    reply = await _feed_command(bot, sent, "/unlock 1234", user_id=999)

    assert "Доступ запрещён" in reply
    assert llm_spy.called is False
    assert gateway_spy.called is False


@pytest.mark.anyio
async def test_owner_unlock_wrong_pin_reply_and_no_llm_gateway(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")

    bot = _build_bot(tmp_path, monkeypatch)
    llm_spy = _RaisingSpy()
    gateway_spy = _RaisingSpy()
    monkeypatch.setattr("antigona.channels.telegram.bot.chitchat_reply", llm_spy)
    monkeypatch.setattr(bot.gateway_client, "submit", gateway_spy)

    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _make_fake_call(sent))

    reply = await _feed_command(bot, sent, "/unlock 0000", user_id=42)

    assert "Неверный PIN" in reply
    assert llm_spy.called is False
    assert gateway_spy.called is False


def test_unknown_action_falls_back_to_critical() -> None:
    """Step 10/11: классификация риска — ядро (tools/pin_gate), не транспорт.

    Неизвестное действие → CRITICAL (fail-closed) — контракт сохранён.
    """
    from antigona.tools.pin_gate import RiskClass, classify_action

    risk = classify_action("something_new")
    assert risk == RiskClass.CRITICAL
    assert risk.value == "CRITICAL"


@pytest.mark.anyio
async def test_non_owner_pin_blocked_no_elevation_no_llm_gateway(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security review F1/F3: /pin must check owner BEFORE PIN (Phase 7 7.1)."""
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")

    bot = _build_bot(tmp_path, monkeypatch)
    llm_spy = _RaisingSpy()
    gateway_spy = _RaisingSpy()
    monkeypatch.setattr("antigona.channels.telegram.bot.chitchat_reply", llm_spy)
    monkeypatch.setattr(bot.gateway_client, "submit", gateway_spy)

    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _make_fake_call(sent))

    reply = await _feed_command(bot, sent, "/pin 1234", user_id=999)

    assert "Доступ запрещён" in reply
    assert llm_spy.called is False
    assert gateway_spy.called is False

    from antigona.tools import pin_gate

    assert pin_gate.is_verified(999) is False
    assert pin_gate.is_elevated(999) is False


@pytest.mark.anyio
async def test_owner_pin_still_works(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner /pin path must remain functional after the owner-check fix."""
    monkeypatch.setenv("ANTIGONA_OWNER_ID", "42")
    monkeypatch.setenv("ANTIGONA_PIN", "1234")

    bot = _build_bot(tmp_path, monkeypatch)
    llm_spy = _RaisingSpy()
    gateway_spy = _RaisingSpy()
    monkeypatch.setattr("antigona.channels.telegram.bot.chitchat_reply", llm_spy)
    monkeypatch.setattr(bot.gateway_client, "submit", gateway_spy)

    sent: list[TelegramMethod[Any]] = []
    monkeypatch.setattr(Bot, "__call__", _make_fake_call(sent))

    reply = await _feed_command(bot, sent, "/pin 1234", user_id=42)

    assert "PIN принят" in reply
    assert llm_spy.called is False
    assert gateway_spy.called is False

    from antigona.tools import pin_gate

    assert pin_gate.is_verified(955_111) is True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
