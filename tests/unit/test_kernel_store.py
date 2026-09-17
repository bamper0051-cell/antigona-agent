"""M1 Durable Execution Kernel — store-level mandatory scenarios."""
from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.kernel import (
    KernelFenceError,
    KernelStore,
    RunState,
    TaskState,
)


def make_store(tmp_path: Path) -> tuple[KernelStore, str, Database]:
    url = f"sqlite:///{tmp_path / 'kernel.sqlite'}"
    db = Database(url)
    db.create_all()
    store = KernelStore(db.session_factory)
    return store, url, db


def new_store_on(url: str) -> KernelStore:
    db = Database(url)
    return KernelStore(db.session_factory)


def _claim_and_run(store: KernelStore, task_id: str, worker: str, lease=60):
    claimed = store.claim_task(task_id, worker, lease_seconds=lease)
    assert claimed is not None
    return store.create_run(task_id, worker)


# ── 1. State machine ────────────────────────────────────────────────────────


def test_task_state_machine_valid_and_invalid():
    from antigona.kernel.state import run_can_transition, task_can_transition

    assert task_can_transition(TaskState.PENDING, TaskState.READY)
    assert task_can_transition(TaskState.READY, TaskState.RUNNING)
    assert task_can_transition(TaskState.RUNNING, TaskState.SUCCEEDED)
    assert task_can_transition(TaskState.RUNNING, TaskState.READY)  # retry
    # terminal is absorbing
    assert not task_can_transition(TaskState.SUCCEEDED, TaskState.READY)
    assert not task_can_transition(TaskState.FAILED, TaskState.RUNNING)
    # illegal edge
    assert not task_can_transition(TaskState.PENDING, TaskState.SUCCEEDED)

    assert run_can_transition(RunState.READY, RunState.RUNNING)
    assert run_can_transition(RunState.RUNNING, RunState.SUCCEEDED)
    assert run_can_transition(RunState.RUNNING, RunState.LOST)
    assert not run_can_transition(RunState.SUCCEEDED, RunState.FAILED)
    assert not run_can_transition(RunState.LOST, RunState.RUNNING)


# ── 2. Atomic claim — 32 contenders, exactly one winner ────────────────────


def test_atomic_claim_exactly_one_winner(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", kind="t")
    assert task.status == TaskState.READY.value

    winners: list[str] = []
    lock = threading.Lock()

    def try_claim(i: int):
        w = store.claim_task(task.id, f"worker-{i}", lease_seconds=60)
        if w is not None:
            with lock:
                winners.append(f"worker-{i}")

    threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
    claimed = store.get_task(task.id)
    assert claimed.status == TaskState.RUNNING.value
    assert claimed.lease_owner == winners[0]


# ── 3. Lease fencing — stale worker cannot finalize ─────────────────────────


def test_stale_worker_fenced_after_reclaim(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner")
    worker_a = "worker-A"
    run = _claim_and_run(store, task.id, worker_a)

    # Simulate crash: A dies without finalizing; lease expires.
    store.reconcile(
        worker_id="worker-B",
        now=task.created_at + timedelta(hours=1),
    )
    # Run reclaimed as LOST, task back to READY.
    assert store.get_run(run.id).status == RunState.LOST.value
    assert store.get_task(task.id).status == TaskState.READY.value

    # Stale A tries to report SUCCEEDED -> fenced.
    with pytest.raises(KernelFenceError):
        store.finalize_run(run.id, worker_a, RunState.SUCCEEDED)


# ── 4. Crash recovery: kill -9 -> reconcile -> new run -> success ──────────


def test_crash_recovery_full_cycle(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", max_attempts=3)

    # Run 1 starts, worker crashes (no finalize), lease expires.
    _claim_and_run(store, task.id, "worker-A")
    store.reconcile(worker_id="worker-B", now=task.created_at + timedelta(hours=1))

    runs = store.list_runs(task.id)
    assert len(runs) == 1 and runs[0].status == RunState.LOST.value

    # Run 2: worker B retries and succeeds.
    assert store.get_task(task.id).status == TaskState.READY.value
    run2 = _claim_and_run(store, task.id, "worker-B")
    store.finalize_run(
        run2.id, "worker-B", RunState.SUCCEEDED,
        result={"output": "done"}, reason="ok",
    )
    store.complete_task_after_run(task.id, store.get_run(run2.id), "worker-B")

    assert store.get_task(task.id).status == TaskState.SUCCEEDED.value
    history = store.history(task_id=task.id)
    assert any(t.to_state == RunState.LOST.value for t in history)


# ── 5. Restart persistence ──────────────────────────────────────────────────


def test_restart_persistence(tmp_path):
    store, url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", kind="persist", idempotency_key="k1")

    # Simulate process restart: a brand-new store on the same file.
    store2 = new_store_on(url)
    reloaded = store2.get_task(task.id)
    assert reloaded is not None
    assert reloaded.kind == "persist"
    assert reloaded.idempotency_key == "k1"
    assert reloaded.status == TaskState.READY.value


# ── 6. Bounded retry ────────────────────────────────────────────────────────


def test_bounded_retry_three_attempts_then_fail(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", max_attempts=3)

    for attempt in (1, 2, 3):
        run = _claim_and_run(store, task.id, "worker")
        store.finalize_run(
            run.id, "worker", RunState.FAILED,
            error={"code": "E", "message": "boom", "retryable": True},
            reason=f"fail #{attempt}",
        )
        store.complete_task_after_run(task.id, store.get_run(run.id), "worker")

    runs = store.list_runs(task.id)
    assert len(runs) == 3
    assert all(r.status == RunState.FAILED.value for r in runs)
    assert store.get_task(task.id).status == TaskState.FAILED.value
    assert store.get_task(task.id).attempt_count == 3


def test_retry_succeeds_on_second_attempt(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", max_attempts=3)

    run1 = _claim_and_run(store, task.id, "w")
    store.finalize_run(run1.id, "w", RunState.FAILED, error={"retryable": True})
    store.complete_task_after_run(task.id, store.get_run(run1.id), "w")
    assert store.get_task(task.id).status == TaskState.READY.value

    run2 = _claim_and_run(store, task.id, "w")
    store.finalize_run(run2.id, "w", RunState.SUCCEEDED, result={"output": "ok"})
    store.complete_task_after_run(task.id, store.get_run(run2.id), "w")
    assert store.get_task(task.id).status == TaskState.SUCCEEDED.value


def test_non_retryable_fails_immediately(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner", max_attempts=5)
    run = _claim_and_run(store, task.id, "w")
    store.finalize_run(run.id, "w", RunState.FAILED, error={"retryable": False})
    store.complete_task_after_run(task.id, store.get_run(run.id), "w")
    assert store.get_task(task.id).status == TaskState.FAILED.value
    assert len(store.list_runs(task.id)) == 1


# ── 7. Dependencies + cycle detection ───────────────────────────────────────


def test_dependency_child_blocks_until_parent_succeeds(tmp_path):
    store, _url, _db = make_store(tmp_path)
    parent = store.create_task(owner_id="owner", kind="p")
    child = store.create_task(owner_id="owner", kind="c", dependencies=[parent.id])
    assert child.status == TaskState.BLOCKED.value

    # Parent succeeds -> child becomes READY.
    prun = _claim_and_run(store, parent.id, "w")
    store.finalize_run(prun.id, "w", RunState.SUCCEEDED, result={"output": "p"})
    store.complete_task_after_run(parent.id, store.get_run(prun.id), "w")

    assert store.get_task(child.id).status == TaskState.READY.value


def test_dependency_child_cancelled_when_parent_fails(tmp_path):
    store, _url, _db = make_store(tmp_path)
    parent = store.create_task(owner_id="owner", kind="p", max_attempts=1)
    child = store.create_task(owner_id="owner", kind="c", dependencies=[parent.id])
    assert child.status == TaskState.BLOCKED.value

    prun = _claim_and_run(store, parent.id, "w")
    store.finalize_run(prun.id, "w", RunState.FAILED, error={"retryable": False})
    store.complete_task_after_run(parent.id, store.get_run(prun.id), "w")

    assert store.get_task(parent.id).status == TaskState.FAILED.value
    assert store.get_task(child.id).status == TaskState.CANCELLED.value


def test_dependency_cycle_detected(tmp_path):
    store, _url, _db = make_store(tmp_path)
    a = store.create_task(owner_id="owner", kind="a")
    b = store.create_task(owner_id="owner", kind="b", dependencies=[a.id])
    # Wire a -> b to form a <-> b cycle.
    with _db.session_factory() as s:
        from antigona.kernel.models import KernelDependency

        s.add(KernelDependency(child_id=a.id, parent_id=b.id))
        s.commit()
    assert store.detect_cycle(), "expected a cycle to be detected"


# ── 8. Idempotency ──────────────────────────────────────────────────────────


def test_idempotency_returns_existing_task(tmp_path):
    store, _url, _db = make_store(tmp_path)
    t1 = store.create_task(owner_id="owner", kind="t", idempotency_key="unique-1")
    t2 = store.create_task(owner_id="owner", kind="t", idempotency_key="unique-1")
    assert t1.id == t2.id
    assert len(store.history(task_id=t1.id)) >= 1


# ── 9. Cancellation ─────────────────────────────────────────────────────────


def test_cancel_ready_task_directly(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner")
    assert task.status == TaskState.READY.value
    store.request_cancel(task.id)
    assert store.get_task(task.id).status == TaskState.CANCELLED.value


def test_cancel_running_cooperative(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner")
    run = _claim_and_run(store, task.id, "w")
    assert store.get_task(task.id).status == TaskState.RUNNING.value

    store.request_cancel(task.id)
    assert store.is_cancel_requested(task.id) is True
    assert store.get_task(task.id).status == TaskState.RUNNING.value  # not terminal yet

    # Cooperative: worker observes cancel, finalises run CANCELLED, not retried.
    store.finalize_run(run.id, "w", RunState.CANCELLED, reason="cooperative")
    store.complete_task_after_run(task.id, store.get_run(run.id), "w")
    assert store.get_task(task.id).status == TaskState.CANCELLED.value
    assert store.get_task(task.id).attempt_count == 1


# ── 10. Machine-readable history ────────────────────────────────────────────


def test_history_is_append_only_and_readable(tmp_path):
    store, _url, _db = make_store(tmp_path)
    task = store.create_task(owner_id="owner")
    run = _claim_and_run(store, task.id, "w")
    store.finalize_run(run.id, "w", RunState.SUCCEEDED)
    store.complete_task_after_run(task.id, store.get_run(run.id), "w")

    hist = store.history(task_id=task.id)
    # task PENDING->READY, task READY->RUNNING, run READY->RUNNING,
    # run RUNNING->SUCCEEDED, task RUNNING->SUCCEEDED
    assert len(hist) >= 5
    assert all(h.to_state for h in hist)  # machine readable
