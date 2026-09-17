"""Regression tests for Telegram /status 404 vs Gateway failure semantics.

Per Grok contract (GROK_STATUS_DEFECT_TRIAGE.txt):
1. 404 -> typed GatewayFlowNotFoundError -> exact not-found template:
   `📋 Задача <code>{flow_id[:12]}</code> не найдена.`
   parse_mode="HTML", reply_to_message_id preserved, exactly 1 answer, no gateway error log.
2. 200 -> status card template:
   `📋 Задача <code>{flow_id[:12]}</code>: <b>{status}</b>`
3. 5xx / timeout / connect -> generic Gateway error:
   `❌ Не удалось получить статус через Gateway.`
4. Other 4xx (400, 403, 422, etc.) -> generic Gateway error, NOT not-found.
5. /status without args -> list_flows (e.g. empty list -> `📋 Нет задач.`).
6. /get <unknown> and /list <unknown> -> same not-found template.
7. No side-effect POST: unknown id never triggers POST /flows, cancel, steer, LLM.
8. E2E Gate: generic error response when 404 expected must classify as DELIVERY_PASS_STATUS_FAIL (not PASS).
9. E2E Gate negative: not-found text with reply_to matches classifies as PASS.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from antigona.channels.telegram.bot import TelegramBot
from antigona.core.control_plane import FlowStatus
from antigona.core.gateway_client import (
    GatewayClient,
    GatewayFlowNotFoundError,
    GatewayFlowView,
)

# Synthetic test chat/user id. The real owner id is NEVER hard-coded in the
# public tree; production code reads it from ANTIGONA_OWNER_ID (see
# src/antigona/security/owner_identity.py). Override ANTIGONA_TEST_CHAT_ID
# when reproducing a specific deployment.
_SYNTHETIC_CHAT_ID = int(os.environ.get("ANTIGONA_TEST_CHAT_ID", "1000000001"))


def _make_msg(
    text: str,
    message_id: int = 227193,
    chat_id: int = _SYNTHETIC_CHAT_ID,
    user_id: int = _SYNTHETIC_CHAT_ID,
) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.message_id = message_id
    msg.chat.id = chat_id
    msg.from_user.id = user_id
    msg.from_user.is_bot = False
    msg.answer = AsyncMock()
    return msg


def _get_handler(
    bot: TelegramBot, name: str
) -> Callable[..., Coroutine[Any, Any, Any]]:
    for h in bot.router.message.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise ValueError(f"Handler {name} not found on bot router")


# ─── GatewayClient Unit Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_client_get_flow_404_raises_typed_flow_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/flows/unknown-id-123"
        return httpx.Response(404, json={"detail": "Flow not found"}, request=request)

    client = GatewayClient(
        base_url="http://gateway.test",
        token="token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(GatewayFlowNotFoundError) as exc_info:
            await client.get_flow("unknown-id-123")
        assert exc_info.value.flow_id == "unknown-id-123"
        assert "Flow not found: unknown-id-123" in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_client_get_flow_200_returns_flow_view() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/flows/flow-ok-123"
        return httpx.Response(
            200,
            json={
                "id": "flow-ok-123",
                "goal": "do something",
                "target_path": ".",
                "status": "RUNNING",
                "revision": 1,
                "created_at": "2026-09-13T00:00:00",
                "updated_at": "2026-09-13T00:00:00",
                "correlation_id": "corr-1",
            },
            request=request,
        )

    client = GatewayClient(
        base_url="http://gateway.test",
        token="token",
        transport=httpx.MockTransport(handler),
    )
    try:
        view = await client.get_flow("flow-ok-123")
        assert isinstance(view, GatewayFlowView)
        assert view.flow_id == "flow-ok-123"
        assert view.status == FlowStatus.RUNNING
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_client_get_flow_500_raises_http_status_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "Internal Server Error"}, request=request)

    client = GatewayClient(
        base_url="http://gateway.test",
        token="token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_flow("flow-500")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_client_get_flow_400_raises_http_status_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Bad Request"}, request=request)

    client = GatewayClient(
        base_url="http://gateway.test",
        token="token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_flow("flow-400")
    finally:
        await client.close()


# ─── Telegram Bot Status Handler Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_handler_404_not_found(caplog: pytest.LogCaptureFixture) -> None:
    """1. 404 -> exact not-found template, parse_mode=HTML, reply_to matches, no warning log."""
    mock_gw = MagicMock(spec=GatewayClient)
    mock_gw.get_flow = AsyncMock(side_effect=GatewayFlowNotFoundError("nonce_1234567890abcdef"))

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status nonce_1234567890abcdef", message_id=555)
    handler = _get_handler(bot_app, "status_handler")

    with caplog.at_level(logging.WARNING):
        await handler(msg)

    mock_gw.get_flow.assert_awaited_once_with("nonce_1234567890abcdef")
    msg.answer.assert_awaited_once_with(
        "📋 Задача <code>nonce_123456</code> не найдена.",
        parse_mode="HTML",
        reply_to_message_id=555,
    )

    # Must NOT log "Gateway status failed" on 404
    assert not any("Gateway status failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_status_handler_200_status_card() -> None:
    """2. 200 -> exact status card template."""
    mock_gw = MagicMock(spec=GatewayClient)
    mock_flow = MagicMock()
    mock_flow.status = "RUNNING"
    mock_gw.get_flow = AsyncMock(return_value=mock_flow)

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status flow_abcdef123456", message_id=777)
    handler = _get_handler(bot_app, "status_handler")
    await handler(msg)

    mock_gw.get_flow.assert_awaited_once_with("flow_abcdef123456")
    msg.answer.assert_awaited_once_with(
        "📋 Задача <code>flow_abcdef1</code>: <b>RUNNING</b>",
        parse_mode="HTML",
        reply_to_message_id=777,
    )


@pytest.mark.asyncio
async def test_status_handler_5xx_generic_gateway_error(caplog: pytest.LogCaptureFixture) -> None:
    """3. 5xx / timeout / connect -> generic Gateway error message and warning log."""
    mock_gw = MagicMock(spec=GatewayClient)
    req = httpx.Request("GET", "http://127.0.0.1:8090/flows/flow_err")
    resp = httpx.Response(500, request=req)
    mock_gw.get_flow = AsyncMock(side_effect=httpx.HTTPStatusError("Server Error", request=req, response=resp))

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status flow_err", message_id=888)
    handler = _get_handler(bot_app, "status_handler")

    with caplog.at_level(logging.WARNING):
        await handler(msg)

    mock_gw.get_flow.assert_awaited_once_with("flow_err")
    msg.answer.assert_awaited_once_with(
        "❌ Не удалось получить статус через Gateway.",
        parse_mode="HTML",
        reply_to_message_id=888,
    )
    assert any("Gateway status failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_status_handler_other_4xx_generic_error(caplog: pytest.LogCaptureFixture) -> None:
    """4. Other 4xx (400, 403, 422) -> generic Gateway error, NOT not-found."""
    mock_gw = MagicMock(spec=GatewayClient)
    req = httpx.Request("GET", "http://127.0.0.1:8090/flows/flow_bad")
    resp = httpx.Response(400, request=req)
    mock_gw.get_flow = AsyncMock(side_effect=httpx.HTTPStatusError("Bad Request", request=req, response=resp))

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status flow_bad", message_id=999)
    handler = _get_handler(bot_app, "status_handler")

    with caplog.at_level(logging.WARNING):
        await handler(msg)

    mock_gw.get_flow.assert_awaited_once_with("flow_bad")
    msg.answer.assert_awaited_once_with(
        "❌ Не удалось получить статус через Gateway.",
        parse_mode="HTML",
        reply_to_message_id=999,
    )
    assert any("Gateway status failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_status_handler_no_args_list_flows() -> None:
    """5. /status without args -> list_flows."""
    mock_gw = MagicMock(spec=GatewayClient)
    mock_gw.list_flows = AsyncMock(return_value=[])

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status", message_id=111)
    handler = _get_handler(bot_app, "status_handler")
    await handler(msg)

    mock_gw.list_flows.assert_awaited_once_with(limit=10)
    msg.answer.assert_awaited_once_with(
        "📋 Нет задач.",
        parse_mode="HTML",
        reply_to_message_id=111,
    )


@pytest.mark.asyncio
async def test_get_and_list_aliases_not_found() -> None:
    """6. /get <unknown> and /list <unknown> route to status_handler and return not-found."""
    mock_gw = MagicMock(spec=GatewayClient)
    mock_gw.get_flow = AsyncMock(side_effect=GatewayFlowNotFoundError("unknown_flow_id"))

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    # /get <unknown>
    msg_get = _make_msg("/get unknown_flow_id", message_id=222)
    get_handler = _get_handler(bot_app, "get_handler")
    await get_handler(msg_get)
    msg_get.answer.assert_awaited_once_with(
        "📋 Задача <code>unknown_flow</code> не найдена.",
        parse_mode="HTML",
        reply_to_message_id=222,
    )

    # /list <unknown>
    msg_list = _make_msg("/list unknown_flow_id", message_id=333)
    list_handler = _get_handler(bot_app, "list_handler")
    await list_handler(msg_list)
    msg_list.answer.assert_awaited_once_with(
        "📋 Задача <code>unknown_flow</code> не найдена.",
        parse_mode="HTML",
        reply_to_message_id=333,
    )


@pytest.mark.asyncio
async def test_no_side_effect_post_on_unknown_id() -> None:
    """7. Unknown id only queries GET /flows/{id}; never creates flows, cancels, steers or calls LLM."""
    mock_gw = MagicMock(spec=GatewayClient)
    mock_gw.get_flow = AsyncMock(side_effect=GatewayFlowNotFoundError("nonce_query_only"))
    mock_gw.submit = AsyncMock()
    mock_gw.create_flow = AsyncMock()
    mock_gw.cancel = AsyncMock()
    mock_gw.steer = AsyncMock()

    bot_app = TelegramBot(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        gateway_client=mock_gw,
        database_url="sqlite:///:memory:",
    )

    msg = _make_msg("/status nonce_query_only", message_id=444)
    handler = _get_handler(bot_app, "status_handler")
    await handler(msg)

    mock_gw.get_flow.assert_awaited_once_with("nonce_query_only")
    mock_gw.submit.assert_not_called()
    mock_gw.create_flow.assert_not_called()
    mock_gw.cancel.assert_not_called()
    mock_gw.steer.assert_not_called()


# ─── E2E Gate Semantic Classification ─────────────────────────────────────────


def classify_telegram_status_e2e(
    *,
    outbound_msg_id: int,
    inbound_reply_to_id: int,
    inbound_sender_is_bot: bool,
    inbound_text: str,
    nonce: str,
    gateway_status_code: int,
) -> str:
    """Classifies the E2E result strictly per Grok contract.

    Delivery alone is NOT sufficient for PASS.
    """
    if inbound_reply_to_id != outbound_msg_id or not inbound_sender_is_bot:
        return "DELIVERY_FAIL"

    expected_not_found = f"📋 Задача <code>{nonce[:12]}</code> не найдена."
    generic_error = "❌ Не удалось получить статус через Gateway."

    if gateway_status_code == 404:
        if inbound_text.strip() == expected_not_found:
            return "PASS"
        if inbound_text.strip() == generic_error:
            return "DELIVERY_PASS_STATUS_FAIL"
        return "STATUS_FAIL"

    return "PASS" if inbound_text.strip() != generic_error else "STATUS_FAIL"


def test_e2e_gate_rejects_generic_error_on_404() -> None:
    """8. Gate fixture: GET 404 + generic error text + reply_to ok -> DELIVERY_PASS_STATUS_FAIL."""
    outcome = classify_telegram_status_e2e(
        outbound_msg_id=227193,
        inbound_reply_to_id=227193,
        inbound_sender_is_bot=True,
        inbound_text="❌ Не удалось получить статус через Gateway.",
        nonce="ANTIGONA_E2E_CANON_1789293188_c6644bc5",
        gateway_status_code=404,
    )
    assert outcome == "DELIVERY_PASS_STATUS_FAIL"
    assert outcome != "PASS"


def test_e2e_gate_accepts_not_found_on_404() -> None:
    """9. Negative gate fixture: GET 404 + not-found text + reply_to ok -> PASS."""
    nonce = "ANTIGONA_E2E_CANON_1789293188_c6644bc5"
    outcome = classify_telegram_status_e2e(
        outbound_msg_id=227193,
        inbound_reply_to_id=227193,
        inbound_sender_is_bot=True,
        inbound_text=f"📋 Задача <code>{nonce[:12]}</code> не найдена.",
        nonce=nonce,
        gateway_status_code=404,
    )
    assert outcome == "PASS"
