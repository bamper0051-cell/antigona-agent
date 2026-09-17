"""Deadline, cancellation and fail-closed terminal-result contract of the waiter.

These are regression tests for the P1 findings of the terminal-result audit:

* individual HTTP reads were not bounded by the remaining overall deadline, so a
  ``timeout=0.005`` waiter could sit for seconds inside slow reads;
* local cancellation raised immediately, without the mandatory final
  authoritative read that would catch a flow which became terminal meanwhile;
* ``FlowView`` dropped the revision, so a stale ``/result`` could never be
  detected;
* a malformed ``DONE success=true`` without any usable content was reported as a
  success with ``result=None`` and ``error=None``.

Everything runs on ``httpx.MockTransport`` — no sockets, no real waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from antigona.core.control_plane import FlowStatus
from antigona.core.gateway_client import (
    GatewayClient,
    GatewayProtocolError,
    GatewayWaitCancelledError,
    GatewayWaitTimeoutError,
)

NOW = "2026-07-28T12:00:00"

# Long enough that an unbounded read would dominate the test runtime: the
# pre-fix waiter spent two of these before giving up on a 5 ms deadline.
SLOW_READ_SECONDS = 5.0

# Wall-clock ceiling asserted for the bounded cases.  It is ~50x the granted
# grace and ~1/10 of a single slow read, so it stays deterministic on a loaded
# machine while still failing loudly if a read ever becomes unbounded again.
MAX_ELAPSED_SECONDS = 1.0

TINY_GRACE = 0.02


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


def done_result_payload(
    *,
    revision: int = 1,
    success: bool = True,
    safe_result_text: str | None = "42 workspace/file.txt",
    stdout_preview: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "flow_id": "flow-1",
        "status": "DONE",
        "terminal": True,
        "success": success,
        "artifacts": artifacts or [],
        "safe_result_text": safe_result_text,
        "stdout_preview": stdout_preview,
        "failure_reason": None if success else "verified result unavailable",
        "completed_at": NOW,
        "revision": revision,
    }


async def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GatewayClient:
    client = GatewayClient("http://gateway.test", "token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://gateway.test",
        headers={"Authorization": "Bearer token"},
        transport=httpx.MockTransport(handler),
    )
    # Any local-cancellation path that touched the remote cancellation endpoint
    # would mutate durable server state; the waiter must never do that.
    client.cancel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("wait_for_terminal must never cancel remotely")
    )
    return client


def assert_no_state_mutation(seen: list[tuple[str, str]], client: GatewayClient) -> None:
    assert all(method == "GET" for method, _ in seen), seen
    client.cancel.assert_not_awaited()  # type: ignore[attr-defined]
    client.cancel.assert_not_called()  # type: ignore[attr-defined]


# ── Deadline is a hard bound on every read ────────────────────────────────


@pytest.mark.asyncio
async def test_slow_flow_read_cannot_outrun_the_overall_deadline() -> None:
    started = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal started
        started += 1
        await asyncio.sleep(SLOW_READ_SECONDS)
        return httpx.Response(200, json=flow_payload("RUNNING"), request=request)

    client = await make_client(handler)
    begin = time.monotonic()
    try:
        with pytest.raises(GatewayWaitTimeoutError) as caught:
            await client.wait_for_terminal(
                "flow-1",
                timeout=0.005,
                poll_interval=0,
                final_read_grace=TINY_GRACE,
            )
    finally:
        elapsed = time.monotonic() - begin
        await client.close()

    assert caught.value.flow_id == "flow-1"
    assert started >= 1, "the waiter must still issue an authoritative read"
    assert elapsed < MAX_ELAPSED_SECONDS, elapsed


@pytest.mark.asyncio
async def test_slow_result_read_cannot_outrun_the_overall_deadline() -> None:
    """A terminal flow whose ``/result`` hangs must not return a success."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            await asyncio.sleep(SLOW_READ_SECONDS)
            return httpx.Response(200, json=done_result_payload(), request=request)
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    begin = time.monotonic()
    try:
        with pytest.raises(GatewayWaitTimeoutError):
            await client.wait_for_terminal(
                "flow-1",
                timeout=0.005,
                poll_interval=0,
                final_read_grace=TINY_GRACE,
            )
    finally:
        elapsed = time.monotonic() - begin
        await client.close()

    assert elapsed < MAX_ELAPSED_SECONDS, elapsed


@pytest.mark.asyncio
async def test_terminal_observed_at_the_deadline_boundary_is_validated_and_returned() -> None:
    statuses = iter(["RUNNING", "DONE"])
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=done_result_payload(), request=request)
        return httpx.Response(200, json=flow_payload(next(statuses)), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=0, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.result == "42 workspace/file.txt"
    # First observation, mandatory final authoritative read, then /result.
    assert seen == [
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1/result"),
    ]
    assert_no_state_mutation(seen, client)


@pytest.mark.asyncio
async def test_waiting_approval_keeps_polling_and_is_never_terminal() -> None:
    statuses = iter(["WAITING_APPROVAL", "WAITING_APPROVAL", "WAITING_APPROVAL", "DONE"])
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=done_result_payload(), request=request)
        return httpx.Response(200, json=flow_payload(next(statuses)), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert [path for _, path in seen].count("/flows/flow-1") == 4
    assert_no_state_mutation(seen, client)


# ── Local cancellation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_cancellation_performs_a_final_get_and_never_mutates_state() -> None:
    event = asyncio.Event()
    event.set()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=flow_payload("RUNNING"), request=request)

    client = await make_client(handler)
    begin = time.monotonic()
    try:
        with pytest.raises(GatewayWaitCancelledError) as caught:
            await client.wait_for_terminal(
                "flow-1", timeout=5, poll_interval=0, cancel_event=event
            )
    finally:
        elapsed = time.monotonic() - begin
        await client.close()

    assert caught.value.flow_id == "flow-1"
    assert seen == [("GET", "/flows/flow-1"), ("GET", "/flows/flow-1")]
    assert_no_state_mutation(seen, client)
    # Prompt: cancellation must not sit out the remaining 5 s budget.
    assert elapsed < MAX_ELAPSED_SECONDS, elapsed


@pytest.mark.asyncio
async def test_local_cancellation_final_get_wins_the_terminal_race() -> None:
    """Cancelled locally, but the flow had already finished server-side."""

    event = asyncio.Event()
    event.set()
    statuses = iter(["RUNNING", "DONE"])
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=done_result_payload(), request=request)
        return httpx.Response(200, json=flow_payload(next(statuses)), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal(
            "flow-1", timeout=5, poll_interval=0, cancel_event=event
        )
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.result == "42 workspace/file.txt"
    assert terminal.error is None
    assert seen == [
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1"),
        ("GET", "/flows/flow-1/result"),
    ]
    assert_no_state_mutation(seen, client)


@pytest.mark.asyncio
async def test_local_cancellation_terminates_promptly_without_remaining_budget() -> None:
    """No budget and a hanging Gateway: still typed, still prompt, still GET-only."""

    event = asyncio.Event()
    event.set()
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        await asyncio.sleep(SLOW_READ_SECONDS)
        return httpx.Response(200, json=flow_payload("RUNNING"), request=request)

    client = await make_client(handler)
    begin = time.monotonic()
    try:
        with pytest.raises(GatewayWaitCancelledError):
            await client.wait_for_terminal(
                "flow-1",
                timeout=0,
                poll_interval=0,
                cancel_event=event,
                final_read_grace=0,
            )
    finally:
        elapsed = time.monotonic() - begin
        await client.close()

    assert elapsed < MAX_ELAPSED_SECONDS, elapsed
    assert_no_state_mutation(seen, client)


@pytest.mark.asyncio
async def test_local_cancellation_with_a_failing_final_read_stays_cancellation() -> None:
    event = asyncio.Event()
    event.set()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if len(seen) == 1:
            return httpx.Response(200, json=flow_payload("RUNNING"), request=request)
        return httpx.Response(503, json={"detail": "unavailable"}, request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayWaitCancelledError):
            await client.wait_for_terminal(
                "flow-1", timeout=5, poll_interval=0, cancel_event=event
            )
    finally:
        await client.close()

    assert_no_state_mutation(seen, client)


# ── Fail-closed terminal results ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_success_without_text_or_artifact_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(safe_result_text=None, stdout_preview=None),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert "without any usable" in str(caught.value)


@pytest.mark.asyncio
async def test_done_success_with_whitespace_only_text_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(safe_result_text="   \n\t  "),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError):
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_done_success_with_zero_byte_verified_artifact_is_rejected() -> None:
    """The empty-stdout shape from the audit: verified, but nothing to present."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(
                    safe_result_text=None,
                    artifacts=[
                        {
                            "path": "workspace/out.txt",
                            "sha256": "a" * 64,
                            "size": 0,
                            "verified": True,
                        }
                    ],
                ),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError):
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_done_success_with_unverified_artifact_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(
                    safe_result_text=None,
                    artifacts=[
                        {
                            "path": "workspace/out.txt",
                            "sha256": "a" * 64,
                            "size": 42,
                            "verified": False,
                        }
                    ],
                ),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError):
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_done_success_with_a_verified_artifact_only_is_presentable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(
                    safe_result_text=None,
                    artifacts=[
                        {
                            "path": "workspace/out.txt",
                            "sha256": "b" * 64,
                            "size": 42,
                            "verified": True,
                        }
                    ],
                ),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.status is FlowStatus.DONE
    assert terminal.error is None
    # Never both empty: a success must always carry something presentable.
    assert terminal.result is not None
    assert "workspace/out.txt" in terminal.result
    assert "42 bytes" in terminal.result


@pytest.mark.asyncio
async def test_done_success_with_stdout_preview_only_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                json=done_result_payload(
                    safe_result_text=None, stdout_preview="42 workspace/file.txt"
                ),
                request=request,
            )
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        terminal = await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert terminal.result == "42 workspace/file.txt"
    assert terminal.error is None


@pytest.mark.asyncio
async def test_malformed_revision_in_a_result_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            body = done_result_payload()
            body["revision"] = "1"
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(200, json=flow_payload("DONE"), request=request)

    client = await make_client(handler)
    try:
        with pytest.raises(GatewayProtocolError) as caught:
            await client.wait_for_terminal("flow-1", timeout=1, poll_interval=0)
    finally:
        await client.close()

    assert "revision must be an integer" in str(caught.value)


@pytest.mark.asyncio
async def test_typed_result_dto_preserves_the_raw_revision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=done_result_payload(revision=11), request=request)

    client = await make_client(handler)
    try:
        typed = await client.get_result("flow-1")
    finally:
        await client.close()

    assert typed.revision == 11
    assert typed.status is FlowStatus.DONE
    assert typed.terminal is True
    assert typed.success is True
