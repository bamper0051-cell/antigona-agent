"""Durable Execution Kernel (M1) — dispatcher.

Long-running component that drives Tasks/Runs to terminal states:

* on every tick it runs **reconciliation** (crash recovery: reclaim expired
  RUNNING runs as LOST, advance their tasks) and then
* scans READY tasks, atomically claims one (exactly one winner across all
  dispatchers/workers), creates a Run, executes it via :class:`KernelExecutor`
  (policy boundary is never bypassed), finalises the Run (fenced) and advances
  the Task (SUCCEEDED / bounded retry / FAILED).

Restart-safe: state lives in durable storage; a killed dispatcher's in-flight
runs are reclaimed by the next dispatcher that starts. Dispatcher is NOT a
privileged backdoor — the executor enforces PolicyEngine + ApprovalGrant.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from antigona.kernel.executor import KernelExecutor
from antigona.kernel.state import RunState
from antigona.kernel.store import KernelStore

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"dispatcher-{uuid.uuid4().hex[:8]}"


class KernelDispatcher:
    def __init__(
        self,
        store: KernelStore,
        executor: KernelExecutor | None = None,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        poll_interval: float = 0.5,
        batch: int = 10,
    ) -> None:
        self.store = store
        self.executor = executor or KernelExecutor()
        self.worker_id = worker_id or _worker_id()
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.batch = batch
        self._stop = asyncio.Event()

    async def reconcile(self) -> int:
        """Crash recovery: reclaim expired runs, advance their tasks."""
        return self.store.reconcile(
            worker_id=self.worker_id, lease_seconds=self.lease_seconds
        )

    async def _renew_loop(self, run_id: str, task_id: str) -> None:
        """Keep the run+task leases alive while a long Run executes (case E:
        a live worker must never be reclaimed mid-flight and re-executed).
        Transient store errors are logged and retried; the loop only stops when
        the run is terminal or was reclaimed."""
        interval = max(1.0, self.lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.store.renew_run_lease(
                    run_id, self.worker_id, self.lease_seconds
                ):
                    break  # run finished or was reclaimed — stop renewing
                if not self.store.renew_lease(
                    task_id, self.worker_id, self.lease_seconds
                ):
                    break
            except Exception as exc:  # noqa: BLE001 — keep heartbeating through transient errors
                logger.warning("lease renew transient error: %s", exc)

    async def _execute_one(self, task_id: str) -> bool:
        """Claim a READY task, run it, finalise atomically. True if a run ran."""
        claimed = self.store.claim_task(
            task_id, self.worker_id, lease_seconds=self.lease_seconds
        )
        if claimed is None:
            return False  # someone else won the claim — skip
        # Cancelled between being listed READY and claim -> cancel now (never
        # leave it RUNNING holding a lease — review case C).
        if self.store.is_cancel_requested(task_id):
            self.store.cancel_claimed_task(claimed.id, self.worker_id)
            return False
        try:
            run = self.store.create_run(
                claimed.id, self.worker_id, lease_seconds=self.lease_seconds
            )
        except Exception as exc:
            # Never leave a claimed task RUNNING with no run (review case A).
            logger.warning("create_run failed for %s: %s", task_id, exc)
            self.store.release_lease(claimed.id, self.worker_id)
            return False

        renewer = asyncio.create_task(self._renew_loop(run.id, claimed.id))
        try:
            outcome = await self.executor.execute_run(
                task=claimed,
                actor=self.worker_id,
                session_id=f"kernel:{claimed.id}",
                run_id=run.id,
            )
        finally:
            renewer.cancel()
            try:
                await renewer
            except asyncio.CancelledError:
                pass

        if outcome.get("success"):
            to_state = RunState.SUCCEEDED
            result = outcome.get("result") or {}
            error = None
            reason = "run succeeded"
        else:
            to_state = RunState.FAILED
            result = None
            error = outcome.get("error") or {
                "code": "FAILED", "message": "run failed", "retryable": False,
            }
            reason = f"{outcome.get('outcome')}: {error.get('message', '')}"
        try:
            # Atomic: fenced finalize + task advancement in one transaction
            # (review case B: no crash window between the two).
            self.store.finalize_and_complete(
                claimed.id,
                run.id,
                self.worker_id,
                to_state,
                result=result,
                error=error,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — KernelFenceError etc.
            logger.warning("finalize_and_complete failed for run %s: %s", run.id, exc)
        return True

    async def tick(self) -> int:
        """One dispatch pass. Returns the number of runs executed."""
        await self.reconcile()
        executed = 0
        for task in self.store.list_ready_tasks(limit=self.batch):
            if self.store.is_cancel_requested(task.id):
                continue
            if await self._execute_one(task.id):
                executed += 1
        return executed

    async def run_forever(self) -> None:
        """Dispatch loop until :meth:`stop` is called."""
        from antigona.observability_legacy import record as _legacy_record
        _legacy_record("kernel.dispatcher.run_forever")
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — a bad task must not kill the loop
                logger.exception("kernel dispatcher tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
