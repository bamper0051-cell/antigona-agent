"""M1 regressions for the independent review findings (cases A–E).

Each reproduces a hang/duplicate that the reviewer found; these tests pin the
fixes so they cannot regress.

* A: crash between claim_task and create_run -> task must not park RUNNING.
* B: crash between finalize_run(SUCCEEDED) and complete_task_after_run -> task
      must not park RUNNING.
* C: cancel requested during a retryable failing run -> CANCELLED, not parked
      READY (filtered out of the ready scan forever).
* D: multi-parent child with one parent failed -> CANCELLED, not BLOCKED forever.
* E: run lease renewed during execution -> live run is never reclaimed.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


def make_env(tmp_path: Path, db_file: str = "k.sqlite"):
    url = f"sqlite:///{tmp_path / db_file}"
    db = Database(url)
    db.create_all()
    store = KernelStore(db.session_factory)
    grants = ApprovalGrantStore(db_path=str(tmp_path / "grants.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)
    return store, policy, grants, db


def future(hours: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


# ── Case A: claim without create_run ────────────────────────────────────────


def test_case_a_claim_without_run_is_recovered(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "a"})
    store.claim_task(t.id, "worker-A", lease_seconds=5)
    assert len(store.list_runs(t.id)) == 0  # crashed before create_run
    assert store.get_task(t.id).status == TaskState.RUNNING.value

    # A reconciler (later) must not leave it RUNNING forever.
    store.reconcile(worker_id="worker-B", lease_seconds=5, now=future())
    st = store.get_task(t.id).status
    assert st in (TaskState.READY.value, TaskState.SUCCEEDED.value), f"got {st}"


# ── Case B: finalize without complete_task ──────────────────────────────────


def test_case_b_finalized_run_without_task_complete_is_recovered(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "b"})
    store.claim_task(t.id, "worker-A", lease_seconds=5)
    run = store.create_run(t.id, "worker-A")
    store.finalize_run(run.id, "worker-A", RunState.SUCCEEDED, result={"output": "ok"})
    # worker-A crashed before complete_task_after_run -> task stuck RUNNING.
    assert store.get_task(t.id).status == TaskState.RUNNING.value

    store.reconcile(worker_id="worker-B", lease_seconds=5, now=future())
    assert store.get_task(t.id).status == TaskState.SUCCEEDED.value


# ── Case C: cancel requested during a retryable run ─────────────────────────


def test_case_c_cancel_during_retryable_run_not_parked(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "c"},
                          retryable=True, max_attempts=5)
    store.claim_task(t.id, "worker-A", lease_seconds=5)
    run = store.create_run(t.id, "worker-A")
    store.request_cancel(t.id)
    finalised = store.finalize_run(
        run.id, "worker-A", RunState.FAILED,
        error={"code": "X", "message": "boom", "retryable": True},
    )
    store.complete_task_after_run(t.id, finalised, "worker-A")

    st = store.get_task(t.id).status
    assert st == TaskState.CANCELLED.value, f"got {st} (not parked READY)"


# ── Case D: multi-parent child with a failed parent ─────────────────────────


def test_case_d_child_cancelled_when_any_parent_fails(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    p1 = store.create_task(owner_id="o", kind="read_file",
                           payload={"tool_name": "read_file", "path": "p1"})
    p2 = store.create_task(owner_id="o", kind="read_file",
                           payload={"tool_name": "read_file", "path": "p2"})
    child = store.create_task(owner_id="o", kind="read_file",
                              payload={"tool_name": "read_file", "path": "child"},
                              dependencies=[p1.id, p2.id])
    assert store.get_task(child.id).status == TaskState.BLOCKED.value

    # p1 succeeds
    store.claim_task(p1.id, "w", lease_seconds=5)
    r1 = store.create_run(p1.id, "w")
    store.finalize_and_complete(p1.id, r1.id, "w", RunState.SUCCEEDED, result={"output": "1"})
    # p2 fails -> child has a terminally-failed parent -> CANCELLED
    store.claim_task(p2.id, "w", lease_seconds=5)
    r2 = store.create_run(p2.id, "w")
    store.finalize_and_complete(p2.id, r2.id, "w", RunState.FAILED,
                                error={"code": "E", "message": "fail", "retryable": False})
    assert store.get_task(child.id).status == TaskState.CANCELLED.value


# ── Case E: run lease renewal prevents reclaim ──────────────────────────────


def test_case_e_renewed_run_lease_not_reclaimed(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "e"})
    store.claim_task(t.id, "worker-A", lease_seconds=5)
    run = store.create_run(t.id, "worker-A", lease_seconds=2)
    now0 = datetime.now(UTC)

    # Renew at t+1.5 (before the 2s lease expires), then renew again.
    t1 = now0 + timedelta(seconds=1.5)
    assert store.renew_run_lease(run.id, "worker-A", lease_seconds=2, now=t1) is True
    t2 = t1 + timedelta(seconds=1.5)
    assert store.renew_run_lease(run.id, "worker-A", lease_seconds=2, now=t2) is True

    # A reconciler at t3 (still within the renewed lease) must NOT reclaim it.
    t3 = t2 + timedelta(seconds=1.5)
    expired = store.find_expired_runs(now=t3)
    assert run.id not in [r[0] for r in expired]

    # Without renewal, the lease would have expired and it WOULD be reclaimed.
    t4 = t3 + timedelta(seconds=1.5)  # beyond the last renewal (t2+2s)
    expired2 = store.find_expired_runs(now=t4)
    assert run.id in [r[0] for r in expired2]


def test_case_e_long_run_finalizes_despite_longer_than_lease(tmp_path):
    """A run longer than lease_seconds still finalises: the dispatcher renews
    the run lease while it executes (live worker never reclaimed mid-flight)."""
    calls = {"n": 0}

    async def slow(payload, context):
        calls["n"] += 1
        await asyncio.sleep(2.5)  # longer than lease_seconds=1
        return {"output": "done"}

    store, policy, grants, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "slow"},
                          max_attempts=2)
    disp = KernelDispatcher(
        store,
        KernelExecutor(policy_engine=policy, grant_store=grants, handler=slow),
        lease_seconds=1,
    )
    asyncio.run(disp.tick())
    assert store.get_task(t.id).status == TaskState.SUCCEEDED.value
    runs = store.list_runs(t.id)
    assert runs[0].status == RunState.SUCCEEDED.value


# ── Atomic finalize_and_complete ────────────────────────────────────────────


def test_finalize_and_complete_is_one_transaction(tmp_path):
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "atomic"})
    store.claim_task(t.id, "w", lease_seconds=5)
    run = store.create_run(t.id, "w")
    store.finalize_and_complete(t.id, run.id, "w", RunState.SUCCEEDED,
                                result={"output": "ok"})
    assert store.get_run(run.id).status == RunState.SUCCEEDED.value
    assert store.get_task(t.id).status == TaskState.SUCCEEDED.value


def test_finalize_and_complete_stale_worker_fenced(tmp_path):
    from antigona.kernel.store import KernelFenceError

    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "fence"})
    store.claim_task(t.id, "wA", lease_seconds=5)
    run = store.create_run(t.id, "wA")
    # A reclaimer reconciles later (run lease expired) -> steals ownership.
    store.reconcile(worker_id="wB", lease_seconds=5, now=future())
    with pytest.raises(KernelFenceError):
        store.finalize_and_complete(t.id, run.id, "wA", RunState.SUCCEEDED,
                                    result={"output": "stale"})


# ── Review round 2: CANCELLED parents + cancel-during-LOST ──────────────────


def test_parent_cancelled_cancels_child(tmp_path):
    """A CANCELLED parent must cancel its dependents (never leave them BLOCKED
    forever waiting on a parent that will never succeed)."""
    store, _, _, _ = make_env(tmp_path)
    p = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "p"})
    c = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "c"},
                          dependencies=[p.id])
    assert store.get_task(c.id).status == TaskState.BLOCKED.value
    store.request_cancel(p.id)  # READY parent -> CANCELLED -> re-evaluate
    assert store.get_task(p.id).status == TaskState.CANCELLED.value
    assert store.get_task(c.id).status == TaskState.CANCELLED.value


def test_cancel_during_run_lost_reconciles_to_cancelled(tmp_path):
    """Cancel requested during a RUNNING run whose lease then expires (LOST):
    the task must be CANCELLED, not left READY+cancel_requested (which the
    ready-scan filters out forever)."""
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "lost"},
                          retryable=True, max_attempts=5)
    store.claim_task(t.id, "wA", lease_seconds=5)
    store.create_run(t.id, "wA")
    store.request_cancel(t.id)  # RUNNING stays RUNNING, cancel_requested=True
    store.reconcile(worker_id="wB", lease_seconds=5, now=future())
    assert store.get_task(t.id).status == TaskState.CANCELLED.value


def test_cancel_claimed_task_fenced_and_releases_lease(tmp_path):
    """Dispatcher cancelling a task claimed-but-not-yet-run never leaves it
    RUNNING holding a lease."""
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "c2"})
    store.claim_task(t.id, "wA", lease_seconds=5)
    assert store.get_task(t.id).status == TaskState.RUNNING.value
    assert store.cancel_claimed_task(t.id, "wA") is True
    t = store.get_task(t.id)
    assert t.status == TaskState.CANCELLED.value
    assert t.lease_owner is None


# ── Review round 3: cascading dependency cancellation (multi-level) ─────────


def test_cascade_cancel_chain_A_B_C(tmp_path):
    """Cancelling A must cascade: B cancelled, then C cancelled (grandchild not
    left BLOCKED forever)."""
    store, _, _, _ = make_env(tmp_path)
    a = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "a"})
    b = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "b"},
                          dependencies=[a.id])
    c = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "c"},
                          dependencies=[b.id])
    assert store.get_task(b.id).status == TaskState.BLOCKED.value
    assert store.get_task(c.id).status == TaskState.BLOCKED.value
    store.request_cancel(a.id)
    assert store.get_task(a.id).status == TaskState.CANCELLED.value
    assert store.get_task(b.id).status == TaskState.CANCELLED.value
    assert store.get_task(c.id).status == TaskState.CANCELLED.value


def test_cascade_cancel_diamond(tmp_path):
    """Diamond A->(B,C)->D: cancelling A cancels B, C and D (D never BLOCKED
    forever even though it also depends on C which only gets cancelled via the
    cascade)."""
    store, _, _, _ = make_env(tmp_path)
    a = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "a"})
    b = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "b"},
                          dependencies=[a.id])
    c = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "c"},
                          dependencies=[a.id])
    d = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "d"},
                          dependencies=[b.id, c.id])
    store.request_cancel(a.id)
    assert store.get_task(b.id).status == TaskState.CANCELLED.value
    assert store.get_task(c.id).status == TaskState.CANCELLED.value
    assert store.get_task(d.id).status == TaskState.CANCELLED.value


def test_orphaned_claim_counts_attempt_so_create_run_spin_is_bounded(tmp_path):
    """A task whose create_run always fails must not claim->recover->claim
    forever: each orphaned claim consumes an attempt, so it eventually FAILs."""
    store, _, _, _ = make_env(tmp_path)
    t = store.create_task(owner_id="o", kind="read_file",
                          payload={"tool_name": "read_file", "path": "spin"},
                          retryable=True, max_attempts=3)
    for _ in range(3):
        store.claim_task(t.id, "wA", lease_seconds=5)  # claim, then crash before create_run
        store.release_lease(t.id, "wA")  # dispatcher releases on create_run failure
        # A reconcile later recovers the RUNNING task (lease released) -> +1 attempt.
        store.reconcile(worker_id="wB", lease_seconds=5,
                        now=datetime.now(UTC) + timedelta(hours=1))
    assert store.get_task(t.id).status == TaskState.FAILED.value
