"""NEXT-1B: the ``requires_approval`` enforcement gate in ``ToolRegistry.dispatch``.

Contract: "allowed=true" from the policy engine is NOT a substitute for approval.
A tool declared ``requires_approval=True`` must NOT execute without a real
durable one-shot approval grant bound to the caller, tool, and exact args — an
arbitrary non-empty string is not approval.  A denied call produces no handler
side effect, a denial to the caller, a DENIED audit record, and no SUCCESS
record; a consumed grant does not replay.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from antigona.security.approval_grant import ApprovalGrantStore
from antigona.tools.registry import ToolRegistry


class _AllowAllPolicy:
    """Policy engine stub that always allows — the exact regression scenario."""

    async def check(self, **kwargs: Any) -> dict[str, Any]:
        return {"allowed": True}


class _RecordingAudit:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def log_action(self, **kwargs: Any) -> None:
        self.actions.append(kwargs)


def _registry_with(
    tmp_path: Path,
    calls: list[dict[str, Any]],
    *,
    requires_approval: bool,
) -> tuple[ToolRegistry, _RecordingAudit, ApprovalGrantStore]:
    async def handler(**kwargs: Any) -> str:
        calls.append(kwargs)
        return json.dumps({"success": True})

    registry = ToolRegistry()
    registry.register(
        "guarded_tool",
        toolset="test",
        schema={"type": "object", "properties": {}},
        handler=handler,
        requires_approval=requires_approval,
        replace=True,
    )
    audit = _RecordingAudit()
    store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    registry.policy_engine = _AllowAllPolicy()
    registry.audit_logger = audit
    registry.grant_store = store
    return registry, audit, store


def test_register_records_requires_approval_on_tool(tmp_path: Path) -> None:
    registry, _audit, _store = _registry_with(tmp_path, [], requires_approval=True)
    assert registry.get("guarded_tool").requires_approval is True


@pytest.mark.asyncio
async def test_requires_approval_tool_denied_without_token_even_when_policy_allows(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    registry, audit, _store = _registry_with(tmp_path, calls, requires_approval=True)

    raw = await registry.dispatch("guarded_tool")
    result = json.loads(raw)

    # Caller sees a denial, not a result.
    assert result.get("error") == "Tool requires a valid one-shot approval grant"
    assert result.get("requires_approval") is True
    # No handler side effect.
    assert calls == []
    # DENIED audit record, exit_code 403, and no SUCCESS record.
    statuses = [a.get("status") for a in audit.actions]
    assert "DENIED" in statuses
    assert "SUCCESS" not in statuses
    denied = [a for a in audit.actions if a.get("status") == "DENIED"]
    assert denied[0]["exit_code"] == 403


@pytest.mark.asyncio
async def test_blank_approval_token_is_not_approval(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    registry, _audit, _store = _registry_with(tmp_path, calls, requires_approval=True)

    result = json.loads(await registry.dispatch("guarded_tool", approval_token="   "))

    assert result.get("requires_approval") is True
    assert calls == []


@pytest.mark.asyncio
async def test_arbitrary_non_empty_token_is_not_a_grant(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    registry, audit, _store = _registry_with(tmp_path, calls, requires_approval=True)

    result = json.loads(
        await registry.dispatch(
            "guarded_tool", approval_token="grant-123", _user_id="cli-user"
        )
    )

    assert result.get("requires_approval") is True
    assert calls == []
    assert "SUCCESS" not in [a.get("status") for a in audit.actions]


@pytest.mark.asyncio
async def test_requires_approval_tool_runs_with_a_real_bound_grant(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    registry, audit, store = _registry_with(tmp_path, calls, requires_approval=True)
    token = store.issue(
        actor="cli-user",
        tool_name="guarded_tool",
        args={},
        issuer="test-owner-approval",
    )

    result = json.loads(
        await registry.dispatch(
            "guarded_tool", approval_token=token, _user_id="cli-user"
        )
    )

    assert result.get("success") is True
    assert len(calls) == 1
    # The token itself is consumed by dispatch, not leaked into the handler.
    assert "approval_token" not in calls[0]
    assert "SUCCESS" in [a.get("status") for a in audit.actions]


@pytest.mark.asyncio
async def test_real_grant_is_one_shot_and_replay_is_denied(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    registry, _audit, store = _registry_with(tmp_path, calls, requires_approval=True)
    token = store.issue(
        actor="cli-user",
        tool_name="guarded_tool",
        args={},
        issuer="test-owner-approval",
    )

    first = json.loads(
        await registry.dispatch(
            "guarded_tool", approval_token=token, _user_id="cli-user"
        )
    )
    replay = json.loads(
        await registry.dispatch(
            "guarded_tool", approval_token=token, _user_id="cli-user"
        )
    )

    assert first.get("success") is True
    assert replay.get("requires_approval") is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_grant_bound_to_other_actor_is_denied(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    registry, _audit, store = _registry_with(tmp_path, calls, requires_approval=True)
    token = store.issue(
        actor="someone-else",
        tool_name="guarded_tool",
        args={},
        issuer="test-owner-approval",
    )

    result = json.loads(
        await registry.dispatch(
            "guarded_tool", approval_token=token, _user_id="cli-user"
        )
    )

    assert result.get("requires_approval") is True
    assert calls == []


@pytest.mark.asyncio
async def test_tool_without_requires_approval_is_unaffected(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    registry, _audit, _store = _registry_with(tmp_path, calls, requires_approval=False)

    result = json.loads(await registry.dispatch("guarded_tool"))

    assert result.get("success") is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gate_also_reads_requires_approval_from_a_contract_spec(
    tmp_path: Path,
) -> None:
    """A contract tool whose ``spec.requires_approval`` is True is gated too."""
    calls: list[dict[str, Any]] = []

    async def handler(**kwargs: Any) -> str:
        calls.append(kwargs)
        return json.dumps({"success": True})

    class _Spec:
        requires_approval = True

    registry = ToolRegistry()
    registry.register(
        "spec_tool",
        toolset="test",
        schema={"type": "object", "properties": {}},
        handler=handler,
        replace=True,
    )
    registry.get("spec_tool").spec = _Spec()
    registry.policy_engine = _AllowAllPolicy()
    registry.audit_logger = _RecordingAudit()
    registry.grant_store = ApprovalGrantStore(tmp_path / "grants.sqlite")

    result = json.loads(await registry.dispatch("spec_tool"))

    assert result.get("requires_approval") is True
    assert calls == []
