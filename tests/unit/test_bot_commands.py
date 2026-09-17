"""Tests for new bot commands: /help, /update, /agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from antigona.channels.telegram.bot import (
    TelegramBot,
    _build_help_text,
)


@pytest.fixture
def bot_instance() -> TelegramBot:
    """Return a TelegramBot with dummy token (no live connection)."""
    return TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
    )


def _make_message(text: str) -> MagicMock:
    """Build a minimal Message mock with the given text."""
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = 12345
    msg.chat.type = "private"
    msg.message_id = 1
    msg.from_user = MagicMock()
    msg.from_user.id = 99999
    msg.bot = AsyncMock()
    msg.answer = AsyncMock()
    msg.answer.__name__ = "answer"
    msg.html_text = text
    return msg


# ─── _build_help_text ────────────────────────────────────────────────────────


def test_build_help_text_contains_all_sections() -> None:
    """Verify _build_help_text includes all required command sections."""
    text = _build_help_text()
    assert "Antigona Telegram Bot" in text
    assert "Управление" in text
    assert "Задачи (через Gateway)" in text
    assert "Память (через Gateway)" in text
    assert "Контекст" in text


def test_build_help_text_contains_all_commands() -> None:
    """Verify all documented commands appear in the help text."""
    text = _build_help_text()
    commands = [
        "/start", "/help",
        "/status", "/list", "/get", "/cancel", "/steer",
        "/approvals", "/approve", "/deny", "/task",
        "/memory", "/remember", "/forget",
        "/pin", "/unlock", "/lock", "/auth_status", "/confirm",
    ]
    for cmd in commands:
        assert cmd in text, f"Command {cmd} missing from help text"


def test_build_help_text_uses_html_formatting() -> None:
    """Help text should use HTML-compatible formatting."""
    text = _build_help_text()
    assert "<b>" in text
    assert "</b>" in text
    assert "<i>" in text


# ─── /help handler ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_handler_returns_command_list(bot_instance: TelegramBot) -> None:
    """/help should respond with the full command list."""
    message = _make_message("/help")

    with patch.object(bot_instance, "_enforce_rate_limit", new=AsyncMock()):
        await bot_instance.router.message.handlers[1].callback(message)

    message.answer.assert_awaited_once()
    reply_text = message.answer.call_args[1].get("text") or message.answer.call_args[0][0]
    assert "Antigona Telegram Bot" in reply_text
    assert "/start" in reply_text
    assert "/status" in reply_text
    assert "/approve" in reply_text


@pytest.mark.asyncio
async def test_help_handler_html_parse_mode(bot_instance: TelegramBot) -> None:
    """/help should use HTML parse_mode."""
    message = _make_message("/help")

    with patch.object(bot_instance, "_enforce_rate_limit", new=AsyncMock()):
        await bot_instance.router.message.handlers[1].callback(message)

    kwargs = message.answer.call_args[1]
    assert kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_commands_alias_works(bot_instance: TelegramBot) -> None:
    """/commands should work as an alias for /help."""
    message = _make_message("/commands")

    with patch.object(bot_instance, "_enforce_rate_limit", new=AsyncMock()):
        await bot_instance.router.message.handlers[1].callback(message)

    message.answer.assert_awaited_once()
    reply_text = message.answer.call_args[1].get("text") or message.answer.call_args[0][0]
    assert "Antigona Telegram Bot" in reply_text


