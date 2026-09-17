
"""Approval inline-button callback must fit Telegram's 64-byte callback_data cap.

Regression for the "Resulted callback data is too long" render failure: the
approve/reject buttons previously packed both the 36-char approval UUID and the
36-char flow UUID (~86 bytes > 64). We pack only a compact token + action and
resolve the real (approval_id, flow_id) from an in-process registry.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from antigona.channels.telegram.bot import (
    ApprovalCallback,
    TelegramBot,
    make_approval_keyboard,  # noqa: F401
)

OWNER_ID = 987654321


@pytest.fixture(autouse=True)
def _owner_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an authorized owner so approval handlers do not deny."""
    monkeypatch.setenv("ANTIGONA_OWNER_ID", str(OWNER_ID))


def _flow_data(approval_id: str, flow_id: str) -> dict[str, object]:
    """Build a pending-approval flow payload for make_approval_keyboard."""
    return {
        "id": flow_id,
        "approvals": [
            {
                "id": approval_id,
                "risk_level": "MEDIUM",
                "reason": "approval required",
                "decision": "PENDING",
            }
        ],
    }


def _callback_query() -> MagicMock:
    """A minimal CallbackQuery mock usable by the approval handler."""
    from aiogram.types import Message as AiogramMessage

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = OWNER_ID
    query.answer = AsyncMock()
    query.answer.__name__ = "answer"
    msg = MagicMock(spec=AiogramMessage)
    msg.message_id = 10
    msg.text = "card"
    msg.edit_text = AsyncMock()
    query.message = msg
    return query


def _build_bot() -> TelegramBot:
    return TelegramBot(
        token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        gateway_url="http://test-gateway:8090",
        gateway_token="test-token",
    )


async def _run_handler(
    bot: TelegramBot, query: MagicMock, callback_data: ApprovalCallback
) -> None:
    await bot.router.callback_query.handlers[0].callback(
        query, callback_data=callback_data
    )


# ── (a) keyboard callback_data is compact and resolves on round-trip ──────────


def test_keyboard_callback_data_under_64_bytes() -> None:
    approval_id = str(uuid.uuid4())
    flow_id = str(uuid.uuid4())
    kb = make_approval_keyboard(_flow_data(approval_id, flow_id))
    assert kb is not None
    for btn in kb.inline_keyboard[0]:
        assert len(btn.callback_data.encode("utf-8")) <= 64, (
            f"{btn.text} callback_data {len(btn.callback_data.encode('utf-8'))} bytes"
        )


def test_keyboard_callback_round_trip_resolves_ids() -> None:
    from antigona.transport.telegram import resolve_approval_token

    approval_id = str(uuid.uuid4())
    flow_id = str(uuid.uuid4())
    kb = make_approval_keyboard(_flow_data(approval_id, flow_id))
    assert kb is not None
    # The button packs only a token; the full ids are recovered via the registry.
    cb = kb.inline_keyboard[0][0].callback_data
    parsed = ApprovalCallback.unpack(cb)
    entry = resolve_approval_token(parsed.approval_id)
    assert entry is not None
    resolved_approval_id, resolved_flow_id = entry
    assert resolved_approval_id == approval_id
    assert resolved_flow_id == flow_id


# ── (b) handler resolves token → correct downstream call ──────────────────────


@pytest.mark.asyncio
async def test_approve_resolves_token_and_posts_decision() -> None:
    bot = _build_bot()
    approval_id = str(uuid.uuid4())
    flow_id = str(uuid.uuid4())
    kb = make_approval_keyboard(_flow_data(approval_id, flow_id))
    assert kb is not None
    cb_raw = kb.inline_keyboard[0][0].callback_data  # approve button
    callback_data = ApprovalCallback.unpack(cb_raw)
    query = _callback_query()

    with patch.object(
        bot, "post_approval_decision", new=AsyncMock(return_value={"decision": "APPROVED"})
    ) as mock_decision:
        await _run_handler(bot, query, callback_data)

    # flow_id is not needed for approve, but the real approval_id must be used.
    mock_decision.assert_awaited_once_with(approval_id, approve=True)
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_resolves_token_and_requests_cancel() -> None:
    bot = _build_bot()
    approval_id = str(uuid.uuid4())
    flow_id = str(uuid.uuid4())
    kb = make_approval_keyboard(_flow_data(approval_id, flow_id))
    assert kb is not None
    cb_raw = kb.inline_keyboard[0][1].callback_data  # reject button
    callback_data = ApprovalCallback.unpack(cb_raw)
    query = _callback_query()

    bot.event_bus.request_cancel = AsyncMock()
    with patch.object(
        bot, "post_approval_decision", new=AsyncMock(return_value={"decision": "REJECTED"})
    ) as mock_decision:
        await _run_handler(bot, query, callback_data)

    # Reject must cancel the flow using the flow_id resolved from the token.
    bot.event_bus.request_cancel.assert_awaited_once()
    kwargs = bot.event_bus.request_cancel.call_args.kwargs
    assert kwargs["task_id"] == flow_id
    assert "approval rejection" in kwargs["reason"]
    mock_decision.assert_awaited_once_with(approval_id, approve=False)


# ── (c) unknown/expired token fails gracefully ────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_token_fails_gracefully() -> None:
    bot = _build_bot()
    # A well-formed token that was never registered (e.g. after a bot restart).
    unknown_token = "0123456789abcdef"
    callback_data = ApprovalCallback(approval_id=unknown_token, action="reject")
    query = _callback_query()

    bot.event_bus.request_cancel = AsyncMock()
    with patch.object(bot, "post_approval_decision", new=AsyncMock()) as mock_decision:
        await _run_handler(bot, query, callback_data)

    mock_decision.assert_not_awaited()
    bot.event_bus.request_cancel.assert_not_awaited()
    query.answer.assert_awaited_once()
    text = query.answer.call_args.kwargs.get("text", "") or query.answer.call_args[0][0]
    assert "устарел" in text or "недействительн" in text
