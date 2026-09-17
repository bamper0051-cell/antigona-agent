"""M1 Durable Execution Kernel — executor security + dispatcher behaviour."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.kernel import (
    KernelDispatcher,
    KernelExecutor,
    KernelStore,
    RunState,
    TaskState,
)
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore


def make_env(tmp_path: Path, grant_db: str | None = None):
    url = f"sqlite:///{tmp_path / 'kernel.sqlite'}"
    db = Database(url)
    db.create_all()
    store = KernelStore(db.session_factory)
    grants = ApprovalGrantStore(db_path=grant_db or str(tmp_path / "grants.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)
    return store, policy, grants, db


# ── Security: M0 policy/approval not bypassed by the kernel ────────────────


@pytest.mark.asyncio
async def test_executor_denies_policy_violation(tmp_path):
    """A task whose payload violates policy must be POLICY_DENIED (no bypass)."""
    store, policy, grants, _db = make_env(tmp_path)
    exe = KernelExecutor(policy_engine=policy, grant_store=grants)

    task = store.create_task(
        owner_id="owner",
        kind="run_shell",
        payload={"tool_name": "run_shell", "command": "rm -rf /tmp/x"},
    )
    out = await exe.execute_run(task=task, actor="kernel")
    assert out["success"] is False
    assert out["outcome"] in ("POLICY_DENIED", "APPROVAL_REQUIRED")
    assert out["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_executor_critical_requires_approval_grant(tmp_path):
    """A CRITICAL action without a grant is APPROVAL_REQUIRED (never executed)."""
    store, policy, grants, _db = make_env(tmp_path)
    exe = KernelExecutor(policy_engine=policy, grant_store=grants)

    # run_shell with a destructive command is CRITICAL -> 2-step required.
    task = store.create_task(
        owner_id="owner",
        kind="run_shell",
        payload={"tool_name": "run_shell", "command": "rm -rf /tmp/critical"},
    )
    out = await exe.execute_run(task=task, actor="owner")
    assert out["outcome"] == "APPROVAL_REQUIRED"

    # Re-issue with a valid consumed grant -> executes.
    token = grants.issue(
        actor="owner", tool_name="run_shell",
        args={"command": "rm -rf /tmp/critical", "tool_name": "run_shell"},
        issuer="test",
    )
    task.payload = dict(task.payload) | {"approval_token": token}
    out2 = await exe.execute_run(task=task, actor="owner")
    assert out2["success"] is True and out2["outcome"] == "SUCCEEDED"

    # Replay the same grant -> APPROVAL_REQUIRED (one-shot consumed).
    task.payload = dict(task.payload) | {"approval_token": token}
    out3 = await exe.execute_run(task=task, actor="owner")
    assert out3["outcome"] == "APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_executor_allows_safe_action(tmp_path):
    store, policy, grants, _db = make_env(tmp_path)
    exe = KernelExecutor(policy_engine=policy, grant_store=grants)
    task = store.create_task(
        owner_id="owner", kind="read_file",
        payload={"tool_name": "read_file", "path": "notes.txt"},
    )
    out = await exe.execute_run(task=task, actor="owner")
    assert out["success"] is True and out["outcome"] == "SUCCEEDED"


# ── Dispatcher behaviour ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_normal_success(tmp_path):
    store, policy, grants, _db = make_env(tmp_path)
    task = store.create_task(
        owner_id="owner", kind="read_file",
        payload={"tool_name": "read_file", "path": "a.txt"},
    )
    disp = KernelDispatcher(store, KernelExecutor(policy_engine=policy, grant_store=grants))
    ran = await disp.tick()
    assert ran == 1
    assert store.get_task(task.id).status == TaskState.SUCCEEDED.value
    runs = store.list_runs(task.id)
    assert len(runs) == 1 and runs[0].status == RunState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_two_dispatchers_one_run_executed(tmp_path):
    """Two dispatchers scanning the same READY task -> exactly one executes it."""
    store, policy, grants, _db = make_env(tmp_path)
    task = store.create_task(
        owner_id="owner", kind="read_file", payload={"tool_name": "read_file", "path": "b"}
    )
    d1 = KernelDispatcher(store, KernelExecutor(policy_engine=policy, grant_store=grants))
    d2 = KernelDispatcher(store, KernelExecutor(policy_engine=policy, grant_store=grants))
    await asyncio.gather(d1.tick(), d2.tick())
    runs = store.list_runs(task.id)
    assert len(runs) == 1, f"expected exactly 1 run, got {len(runs)}"
    assert runs[0].status == RunState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_dispatcher_retries_then_succeeds(tmp_path):
    calls = {"n": 0}

    async def flaky(payload, context):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient boom")
        return {"output": "finally ok"}

    store, policy, grants, _db = make_env(tmp_path)
    task = store.create_task(
        owner_id="owner", kind="flaky", payload={"tool_name": "read_file", "path": "c"},
        max_attempts=4,
    )
    disp = KernelDispatcher(
        store,
        KernelExecutor(policy_engine=policy, grant_store=grants, handler=flaky),
    )
    for _ in range(4):
        await disp.tick()

    assert store.get_task(task.id).status == TaskState.SUCCEEDED.value
    runs = store.list_runs(task.id)
    assert len(runs) == 3  # 2 failed attempts + 1 success
    assert [r.status for r in runs] == [
        RunState.FAILED.value, RunState.FAILED.value, RunState.SUCCEEDED.value,
    ]


@pytest.mark.asyncio
async def test_dispatcher_reconciles_crashed_run(tmp_path):
    """A RUNNING run whose lease expired (worker killed) is reclaimed and retried."""
    store, policy, grants, _db = make_env(tmp_path)
    task = store.create_task(owner_id="owner", kind="read_file", payload={"tool_name": "read_file", "path": "d"}, max_attempts=3)

    # Worker A claims + starts a run, then crashes (no finalize). Lease expires.
    claimed = store.claim_task(task.id, "worker-A", lease_seconds=5)
    store.create_run(task.id, "worker-A")
    # Expire the run's lease so reconciliation reclaims it.
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update as sa_update

    from antigona.kernel.models import KernelRun

    run0 = store.list_runs(task.id)[0]
    with _db.session_factory() as s:
        s.execute(
            sa_update(KernelRun)
            .where(KernelRun.id == run0.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        s.commit()
    assert claimed is not None

    # A fresh dispatcher reconciles and runs it to success.
    disp = KernelDispatcher(store, KernelExecutor(policy_engine=policy, grant_store=grants), lease_seconds=5)
    await disp.tick()
    assert store.get_task(task.id).status == TaskState.SUCCEEDED.value
    runs = store.list_runs(task.id)
    assert runs[0].status == RunState.LOST.value  # crashed run reclaimed
    assert runs[-1].status == RunState.SUCCEEDED.value
