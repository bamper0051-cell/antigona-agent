"""Test the canonical Gateway client contract — paths, bodies, DTOs.

These are isolated MockTransport tests that verify *only* the exact HTTP
contract between the core GatewayClient and the real Gateway API.  They
do not test the CLI chat loop, TUI, or any runtime integration.

RED phase: all tests fail because the current GatewayClient has wrong
endpoints (/decide instead of /decision), wrong body shapes (decision:
str instead of approve: bool) and missing methods (list_approvals,
get_approval).

GREEN phase: after fixing gateway_client.py and control_plane.py,
all tests pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from antigona.core.control_plane import ApprovalView, FlowStatus, NormalizedRequest
from antigona.core.gateway_client import (
    GatewayClient,
    GatewayHTTPError,
    GatewayProtocolError,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GatewayClient:
    client = GatewayClient("http://gateway.test", "test-token")
    # Replace the real transport with MockTransport
    client._client = httpx.AsyncClient(
        base_url="http://gateway.test",
        headers={"Authorization": "Bearer test-token"},
        transport=httpx.MockTransport(handler),
    )
    return client


def _approval_view(
    approval_id: str = "ap-1",
    decision: str = "PENDING",
) -> dict[str, Any]:
    return {
        "id": approval_id,
        "tool_name": "sandbox.shell",
        "risk_level": "HIGH",
        "reason": "approval required",
        "decision": decision,
        "decided_by": None,
    }


def _approval_list_entry(approval_id: str = "ap-1") -> dict[str, Any]:
    return {
        "id": approval_id,
        "task_id": "flow-1",
        "tool_name": "sandbox.shell",
        "risk_level": "HIGH",
        "reason": "approval required",
        "created_at": "2026-07-30T12:00:00",
    }


def _approval_list_view(
    items: list[dict[str, Any]] | None = None,
    total: int = 0,
) -> dict[str, Any]:
    return {
        "items": items or [],
        "total": total,
    }


def _flow_summary(flow_id: str = "flow-1", status: str = "DONE") -> dict[str, Any]:
    return {
        "id": flow_id,
        "goal": "count bytes",
        "status": status,
        "revision": 1,
        "created_at": "2026-07-30T12:00:00",
        "updated_at": "2026-07-30T12:00:05",
    }


def _flow_list_view(
    items: list[dict[str, Any]] | None = None,
    total: int = 0,
) -> dict[str, Any]:
    return {
        "items": items or [],
        "total": total,
    }


# ── Approval decision ────────────────────────────────────────────────────────


class TestApprovalDecision:
    """The decision endpoint must be /decision, not /decide, and body ``approve`` bool."""

    async def _check_decision(self, approve: bool) -> None:
        seen: list[tuple[str, str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path), request.read()))
            expected_decision = "APPROVED" if approve else "DENIED"
            return httpx.Response(
                200,
                json=_approval_view("ap-1", decision=expected_decision),
                request=request,
            )

        client = _make_client(handler)
        try:
            result = await client.decide_approval("ap-1", approve=approve)
        finally:
            await client.close()

        assert len(seen) == 1
        method, path, body = seen[0]
        assert method == "POST"
        assert path == "/approvals/ap-1/decision", (
            f"expected /approvals/ap-1/decision, got {path}"
        )
        import json

        parsed = json.loads(body)
        assert parsed == {"approve": approve}, f"body must be {{'approve': bool}}, got {parsed}"
        assert isinstance(result, ApprovalView)
        assert result.approval_id == "ap-1"
        expected_decision = "APPROVED" if approve else "DENIED"
        assert result.decision == expected_decision, (
            f"expected decision={expected_decision}, got {result.decision}"
        )

    @pytest.mark.asyncio
    async def test_decision_approve_uses_correct_endpoint_and_body(self) -> None:
        await self._check_decision(True)

    @pytest.mark.asyncio
    async def test_decision_deny_uses_correct_endpoint_and_body(self) -> None:
        await self._check_decision(False)


# ── List approvals ───────────────────────────────────────────────────────────


class TestListApprovals:
    """GET /approvals with status/limit/offset params, returns ApprovalListView."""

    @pytest.mark.asyncio
    async def test_list_approvals_default_params(self) -> None:
        seen: list[tuple[str, str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path), dict(request.url.params)))
            return httpx.Response(
                200,
                json=_approval_list_view(
                    items=[_approval_list_entry("ap-1")], total=1
                ),
                request=request,
            )

        client = _make_client(handler)
        try:
            result = await client.list_approvals()
        finally:
            await client.close()

        assert len(seen) == 1
        method, path, params = seen[0]
        assert method == "GET"
        assert path == "/approvals"
        assert params.get("status") == "PENDING"
        assert "total" in str(result) or len(result.get("items", [])) > 0

    @pytest.mark.asyncio
    async def test_list_approvals_custom_params(self) -> None:
        seen: list[tuple[str, str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path), dict(request.url.params)))
            return httpx.Response(200, json=_approval_list_view(), request=request)

        client = _make_client(handler)
        try:
            await client.list_approvals(status="ALL", limit=10, offset=5)
        finally:
            await client.close()

        assert len(seen) == 1
        _, _, params = seen[0]
        assert params.get("status") == "ALL"
        assert params.get("limit") == "10"
        assert params.get("offset") == "5"


# ── Get approval ─────────────────────────────────────────────────────────────


class TestGetApproval:
    """GET /approvals/{id} returns a single ApprovalView."""

    @pytest.mark.asyncio
    async def test_get_approval_by_id(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path)))
            return httpx.Response(
                200,
                json=_approval_view("ap-42", decision="APPROVED"),
                request=request,
            )

        client = _make_client(handler)
        try:
            result = await client.get_approval("ap-42")
        finally:
            await client.close()

        assert len(seen) == 1
        method, path = seen[0]
        assert method == "GET"
        assert path == "/approvals/ap-42"
        assert isinstance(result, ApprovalView)
        assert result.approval_id == "ap-42"
        assert result.decision == "APPROVED"


# ── List flows ───────────────────────────────────────────────────────────────


class TestListFlows:
    """list_flows must accept status/limit/offset, not conversation_id."""

    @pytest.mark.asyncio
    async def test_list_flows_with_status_limit_offset(self) -> None:
        seen: list[tuple[str, str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path), dict(request.url.params)))
            return httpx.Response(
                200,
                json=_flow_list_view(
                    items=[_flow_summary("f1")], total=1
                ),
                request=request,
            )

        client = _make_client(handler)
        try:
            _ = await client.list_flows(status=FlowStatus.DONE, limit=10, offset=5)
        finally:
            await client.close()

        assert len(seen) == 1
        _, _, params = seen[0]
        assert "conversation_id" not in params, (
            "list_flows must not use conversation_id parameter"
        )
        assert params.get("status") == "DONE"
        assert params.get("limit") == "10"
        assert params.get("offset") == "5"

    @pytest.mark.asyncio
    async def test_list_flows_omits_empty_status(self) -> None:
        seen: list[tuple[str, str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url.path), dict(request.url.params)))
            return httpx.Response(200, json=_flow_list_view(), request=request)

        client = _make_client(handler)
        try:
            await client.list_flows(status=None)
        finally:
            await client.close()

        _, _, params = seen[0]
        assert "status" not in params or params.get("status") == ""
        assert "limit" in params


# ── Steer is unsupported ─────────────────────────────────────────────────────


class TestSteerSupported:
    """Steer calls POST /flows/{flow_id}/steer and returns FlowView."""

    @pytest.mark.asyncio
    async def test_steer_calls_steer_flow(self) -> None:
        from antigona.core.control_plane import SteeringCommand

        captured_path = ""
        captured_body = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_path, captured_body
            captured_path = request.url.path
            captured_body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"id": "flow-1", "status": "RUNNING", "goal": "g"},
                request=request,
            )

        client = _make_client(handler)
        try:
            res = await client.steer(
                "flow-1",
                SteeringCommand(flow_id="flow-1", command="continue"),
            )
            assert captured_path == "/flows/flow-1/steer"
            assert captured_body == {"message": "continue"}
            assert res.flow_id == "flow-1"
        finally:
            await client.close()



# ── Unknown status ───────────────────────────────────────────────────────────


class TestUnknownStatus:
    """A Gateway returning an unknown status must fail closed, not coerce."""

    @pytest.mark.asyncio
    async def test_unknown_flow_status_raises_protocol_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "flow-1",
                    "goal": "test",
                    "target_path": ".",
                    "status": "DEFINITELY_DONE",
                    "revision": 1,
                    "checkpoint": "accepted",
                    "cancellation_requested": False,
                    "created_at": "2026-07-30T12:00:00",
                    "updated_at": "2026-07-30T12:00:00",
                    "correlation_id": "corr-1",
                    "steps": [],
                    "transitions": [],
                    "artifacts": [],
                    "approvals": [],
                },
                request=request,
            )

        client = _make_client(handler)
        try:
            with pytest.raises(GatewayProtocolError) as caught:
                await client.get_flow("flow-1")
        finally:
            await client.close()

        msg = str(caught.value)
        assert "unsupported" in msg.lower()
        assert not any(
            f"'{known.value}'" in msg for known in FlowStatus
        ), "must not report unknown status as any known state"


# ── No raw errors in error messages ──────────────────────────────────────────


class TestNoRawErrors:
    """Error messages must not contain raw payloads, secrets, or URLs."""

    @pytest.mark.asyncio
    async def test_http_error_has_no_raw_body(self) -> None:
        secret_value = "s3cr3t-c0mmand"

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

        client = _make_client(handler)
        request = NormalizedRequest(
            source="cli",
            correlation_id="corr-1",
            conversation_id="chat-1",
            owner_id="owner",
            user_message="run",
            metadata={
                "action_type": "RUN_SHELL",
                "command": ["printf", f"API_KEY={secret_value}"],
            },
        )
        try:
            with pytest.raises(GatewayHTTPError) as caught:
                await client.submit(request)
        finally:
            await client.close()

        assert secret_value not in str(caught.value)


class TestSteerFlow:
    """POST /flows/{flow_id}/steer contract tests."""

    @pytest.mark.asyncio
    async def test_steer_flow_success(self) -> None:
        captured_path = ""
        captured_body = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_path, captured_body
            captured_path = request.url.path
            captured_body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"id": "f-123", "status": "RUNNING", "goal": "g"},
                request=request,
            )

        client = _make_client(handler)
        try:
            res = await client.steer_flow("f-123", "use python3.11")
            assert captured_path == "/flows/f-123/steer"
            assert captured_body == {"message": "use python3.11"}
            assert res["id"] == "f-123"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_steer_flow_error_400(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"detail": "Steering not allowed for flow in status DONE"},
                request=request,
            )

        client = _make_client(handler)
        try:
            with pytest.raises(GatewayHTTPError) as exc:
                await client.steer_flow("f-123", "steer msg")
            assert "400" in str(exc.value)
        finally:
            await client.close()

