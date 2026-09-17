"""P1-001 / P1-002: HIGH requires_approval, and the executor enforces it.

P1-001 — a HIGH-risk action is classified ``requires_approval=True``;
LOW-risk actions are not forced through that gate.
P1-002 — the shipped KernelExecutor path will not run a HIGH action without
a valid grant; a consumed one-shot grant is not reusable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from antigona.database import Database
from antigona.kernel import KernelExecutor, KernelStore
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore
from antigona.worker.hitl import (
    ConfirmationPolicy,
    ConfirmationPolicyMode,
    RiskLevel,
    evaluate_risk,
)

CTX = {"channel": "cli", "user_id": "owner", "session_id": "s1"}
HIGH_ACTION = "run_shell"
HIGH_PARAMS = {"tool_name": "run_shell", "command": "rm /tmp/p1_high_target"}
LOW_ACTION = "read_file"
LOW_PARAMS = {"tool_name": "read_file", "path": "notes.txt"}


def _env(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'kernel.sqlite'}"
    db = Database(url)
    db.create_all()
    store = KernelStore(db.session_factory)
    grants = ApprovalGrantStore(db_path=str(tmp_path / "grants.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)
    return store, policy, grants


@pytest.mark.asyncio
async def test_p1_001_high_classified_requires_approval(tmp_path: Path) -> None:
    """P1-001: HIGH write to a system path is classified as requiring approval."""
    _store, policy, _grants = _env(tmp_path)
    verdict = await policy.check(
        "write_file",
        params={"path": "/etc/hosts"},
        context=CTX,
    )
    assert verdict["requires_approval"] is True
    assert verdict["allowed"] is False
    assert verdict["risk_level"] == "HIGH"


@pytest.mark.asyncio
async def test_p1_001_high_shell_rm_requires_approval(tmp_path: Path) -> None:
    """P1-001: HIGH (non-CRITICAL) rm is classified requires_approval."""
    _store, policy, _grants = _env(tmp_path)
    verdict = await policy.check(HIGH_ACTION, params=HIGH_PARAMS, context=CTX)
    assert verdict["requires_approval"] is True
    assert verdict["allowed"] is False
    assert verdict["risk_level"] == "HIGH"
    assert verdict.get("requires_2step_confirmation") is False


@pytest.mark.asyncio
async def test_p1_001_low_not_forced_through_high_gate(tmp_path: Path) -> None:
    """P1-001: LOW-risk read is not forced through the HIGH approval gate."""
    _store, policy, _grants = _env(tmp_path)
    verdict = await policy.check(LOW_ACTION, params=LOW_PARAMS, context=CTX)
    assert verdict["requires_approval"] is False
    assert verdict["allowed"] is True
    assert verdict["risk_level"] == "LOW"


def test_p1_001_hitl_high_only_policy() -> None:
    """P1-001 HITL: HIGH_ONLY mode gates HIGH, not LOW."""
    policy = ConfirmationPolicy(mode=ConfirmationPolicyMode.HIGH_ONLY)
    high, _ = evaluate_risk("sandbox.shell", {"command": ["rm", "-f", "x"]})
    low, _ = evaluate_risk("workspace.read_text", {"path": "output.txt"})
    assert high == RiskLevel.HIGH
    assert low == RiskLevel.LOW
    assert policy.should_require_approval(high) is True
    assert policy.should_require_approval(low) is False


@pytest.mark.asyncio
async def test_p1_002_executor_enforces_requires_approval_one_shot(tmp_path: Path) -> None:
    """P1-002: HIGH does not run without a grant; grant is one-shot."""
    store, policy, grants = _env(tmp_path)
    executed = {"n": 0}

    async def handler(payload: dict, context: dict) -> dict:
        executed["n"] += 1
        return {"output": "ran", "handler": "p1_002"}

    exe = KernelExecutor(policy_engine=policy, grant_store=grants, handler=handler)
    task = store.create_task(owner_id="owner", kind=HIGH_ACTION, payload=dict(HIGH_PARAMS))

    out = await exe.execute_run(task=task, actor="owner")
    assert out["success"] is False
    assert out["outcome"] == "APPROVAL_REQUIRED"
    assert executed["n"] == 0

    token = grants.issue(actor="owner", tool_name=HIGH_ACTION, args=dict(HIGH_PARAMS), issuer="test")
    task.payload = dict(task.payload) | {"approval_token": token}
    out2 = await exe.execute_run(task=task, actor="owner")
    assert out2["success"] is True and out2["outcome"] == "SUCCEEDED"
    assert executed["n"] == 1

    task.payload = dict(task.payload) | {"approval_token": token}
    out3 = await exe.execute_run(task=task, actor="owner")
    assert out3["outcome"] == "APPROVAL_REQUIRED"
    assert executed["n"] == 1


@pytest.mark.asyncio
async def test_p1_002_low_runs_without_grant(tmp_path: Path) -> None:
    """P1-002 / P1-001: LOW is not blocked by the HIGH-only grant gate."""
    store, policy, grants = _env(tmp_path)
    executed = {"n": 0}

    async def handler(payload: dict, context: dict) -> dict:
        executed["n"] += 1
        return {"output": "ran"}

    exe = KernelExecutor(policy_engine=policy, grant_store=grants, handler=handler)
    task = store.create_task(owner_id="owner", kind=LOW_ACTION, payload=dict(LOW_PARAMS))
    out = await exe.execute_run(task=task, actor="owner")
    assert out["success"] is True and out["outcome"] == "SUCCEEDED"
    assert executed["n"] == 1
