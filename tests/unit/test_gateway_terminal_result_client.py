from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from antigona.core.control_plane import FlowStatus, NormalizedRequest
from antigona.core.gateway_client import (
    GatewayClient,
    GatewayHTTPError,
    GatewayProtocolError,
    GatewayWaitCancelledError,
    GatewayWaitTimeoutError,
)

NOW = "2026-07-28T12:00:00"


def flow_payload(status: str, *, revision: int = 1) -> dict[str, Any]:
    return {
        "id": "flow-1",
        "goal": "count bytes",
        "target_path": ".",
        "status": status,
        "revision": revision,
        "checkpoint": "accepted",
        "cancellation_requested": False,
        "created_at": NOW,
        "updated_at": NOW,
        "correlation_id": "corr-1",
        "steps": [],
        "transitions": [],
        "artifacts": [],
        "approvals": [],
    }


def result_payload(status: str, *, revision: int = 1) -> dict[str, Any]:
    # ``revision`` defaults to the same value ``flow_payload`` reports on
    # purpose.  Terminal states are absorbing and the task revision only moves
    # on transitions, so a ``/result`` body describing an already-terminal flow
    # must carry the revision that was just observed.  The previous fixture
    # returned revision 2 against a flow at revision 1 — i.e. it encoded exactly
    # the stale/mismatched pairing the client must now reject.
    terminal = status in {"DONE", "FAILED", "BLOCKED", "CANCELLED", "TIMEOUT", "POLICY_DENIED"}
    return {
        "flow_id": "flow-1",
        "status": status,
        "terminal": terminal,
        "success": status == "DONE",
        "artifacts": [],
        "safe_result_text": "42 workspace/file.txt" if status == "DONE" else None,
        "stdout_preview": "42 workspace/file.txt" if status == "DONE" else None,
        "failure_reason": None if status == "DONE" else ("terminal failure" if terminal else None),
        "completed_at": NOW if terminal else None,
        "revision": revision,
    }


async def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> GatewayClient:
    client = GatewayClient("http://gateway.test", "token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://gateway.test",
        headers={"Authorization": "Bearer token"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_submit_201_is_nonterminal_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(201, json=flow_payload("QUEUED"), request=request)

    client = await make_client(handler)
    try:
        accepted = await client.submit(
            NormalizedRequest(
                source="telegram",
                correlation_id="corr-1",
                conversation_id="chat-1",
                owner_id="owner",
                user_message="count bytes",
            )
        )
    finally:
        await client.close()

    assert accepted.status is FlowStatus.QUEUED
    assert accepted.status is not FlowStatus.DONE


@pytest.mark.asyncio
async def test_submit_error_never_logs_or_raises_raw_command_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_value = "synthetic-command-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "command", 1],
                        "msg": f"invalid {secret_value}",
                        "type": "value_error",
                        "input": f"API_KEY={secret_value}",
                    }
                ]
            },
            request=request,
        )

    client = await make_client(handler)
    request = NormalizedRequest(
        source="telegram",
        correlation_id="corr-1",
        conversation_id="chat-1",
        owner_id="owner",
        user_message="run command",
        metadata={
            "action_type": "RUN_SHELL",
            "command": ["printf", f"API_KEY={secret_value}"],
        },
    )
    try:
        with caplog.at_level(logging.ERROR, logger="antigona.core.gateway_client"):
            with pytest.raises(GatewayHTTPError) as caught:
                await client.submit(request)
    finally:
        await client.close()

    assert secret_value not in caplog.text
    assert secret_value not in str(caught.value)
    assert "sha256" in caplog.text


@pytest.mark.asyncio
async def test_waiter_keeps_waiting_at_waiting_approval_then_returns_done() -> None:
    statuses = iter(["WAITING_APPROVAL", "DONE"])
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result_payload("DONE"), request=request)
        return httpx.Response(200, json=flow_payload(next(statuses)), request=request)

    client = await make_client(handler)
    try:
        result = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert result.status is FlowStatus.DONE
    assert result.result == "42 workspace/file.txt"
    assert seen == [
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1/result"),
    ]


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED", "CANCELLED", "TIMEOUT", "POLICY_DENIED"])
@pytest.mark.asyncio
async def test_waiter_recognizes_every_failure_terminal_state(status: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = result_payload(status) if request.url.path.endswith("/result") else flow_payload(status)
        return httpx.Response(200, json=payload, request=request)

    client = await make_client(handler)
    try:
        result = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
        typed = await client.get_result("flow-1")
    finally:
        await client.close()

    assert result.status.value == status
    assert result.status is not FlowStatus.DONE
    assert result.error == "terminal failure"
    assert typed.terminal is True
    assert typed.success is False
    assert calls == 3


@pytest.mark.asyncio
async def test_waiter_deadline_uses_authoritative_final_get_and_raises_typed_timeout() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=flow_payload("WAITING_APPROVAL"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayWaitTimeoutError) as caught:
            await client.wait_for_terminal("flow-1", timeout=0, poll_interval=0)
    finally:
        await client.close()

    assert caught.value.flow_id == "flow-1"
    assert seen == [("GET", "/flows/flow-1"), ("GET", "/flows/flow-1")]


@pytest.mark.asyncio
async def test_waiter_cancellation_is_local_and_never_posts_cancel() -> None:
    event = asyncio.Event()
    event.set()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=flow_payload("RUNNING"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayWaitCancelledError):
            await client.wait_for_terminal(
                "flow-1", timeout=1, poll_interval=0, cancel_event=event
            )
    finally:
        await client.close()

    # Two GETs, not one: local cancellation must be folded into a mandatory
    # final authoritative read before it may claim the flow is unfinished.
    # Bailing out on the first observation (the previous assertion) would drop
    # a flow that became terminal while the caller was giving up.
    assert seen == [("GET", "/flows/flow-1"), ("GET", "/flows/flow-1")]
    assert all(method == "GET" for method, _ in seen)


@pytest.mark.asyncio
async def test_terminal_result_with_matching_revision_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result_payload("DONE", revision=7), request=request)
        return httpx.Response(200, json=flow_payload("DONE", revision=7), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.revision == 7
    assert terminal.result == "42 workspace/file.txt"
    assert terminal.error is None


@pytest.mark.asyncio
async def test_terminal_result_with_stale_revision_fails_closed() -> None:
    """A result describing an older revision than the observed terminal flow."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result_payload("DONE", revision=6), request=request)
        return httpx.Response(200, json=flow_payload("DONE", revision=7), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert "revision" in str(caught.value)


@pytest.mark.asyncio
async def test_payloads_without_revision_stay_backward_compatible() -> None:
    """Older mocked bodies omit ``revision`` entirely — that must still work."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            body = result_payload("DONE")
            body.pop("revision")
            return httpx.Response(200, json=body, request=request)
        body = flow_payload("DONE")
        body.pop("revision")
        return httpx.Response(200, json=body, request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.revision is None
    assert terminal.result == "42 workspace/file.txt"


@pytest.mark.asyncio
async def test_result_status_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result_payload("FAILED"), request=request)
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert "statuses do not match" in str(caught.value)


@pytest.mark.asyncio
async def test_done_success_false_is_a_terminal_failure_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            body = result_payload("DONE")
            body["success"] = False
            body["safe_result_text"] = None
            body["stdout_preview"] = None
            body["failure_reason"] = "verified result unavailable"
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.result is None
    assert terminal.error == "verified result unavailable"


@pytest.mark.asyncio
async def test_unknown_status_is_never_coerced_to_a_known_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=flow_payload("DEFINITELY_DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    message = str(caught.value)
    assert "unsupported flow status" in message
    assert not any(
        f"'{known.value}'" in message for known in FlowStatus
    ), "an unknown status must not be reported as any known state"
