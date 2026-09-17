"""Autonomous Goal Orchestration (M2) — Goal Engine loop.

The durable orchestrator: owns the Goal lifecycle and drives M1 kernel tasks
through the multi-service pool.

Each tick:
1. kernel reconciliation (M1 crash recovery);
2. wake processing (resume WAITING/BLOCKED goals from durable events/timers);
3. judge every active goal and advance it (DONE/CONTINUE/WAIT/REPLAN/BLOCKED/FAIL);
4. kernel dispatcher tick (execute READY tasks through the service handler).

Everything is persisted; a killed engine is replaced by the next start
(restart-safe, E2E-2). WAIT is durable and releases the worker (E2E-3).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from antigona.kernel import (
    KernelDispatcher,
    KernelExecutor,
    KernelStore,
    TaskState,
)
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore

from .executors import ServiceExecutors
from .judge import GoalJudge
from .models import Goal, GoalFlow
from .planner import GoalPlanner
from .router import ServiceRouter
from .service_handler import ServiceTaskHandler
from .state import (
    FlowState,
    GoalState,
    JudgeDecision,
)
from .store import OrchestrationStore
from .wake import WakeManager

logger = logging.getLogger(__name__)


class GoalEngine:
    def __init__(
        self,
        kernel_store: KernelStore,
        orch: OrchestrationStore,
        *,
        router: ServiceRouter | None = None,
        executors: ServiceExecutors | None = None,
        planner: GoalPlanner | None = None,
        judge: GoalJudge | None = None,
        wake: WakeManager | None = None,
        policy_engine: PolicyEngine | None = None,
        grant_store: ApprovalGrantStore | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        poll_interval: float = 1.0,
        wait_retry_seconds: int = 60,
        failover_wait_seconds: int = 30,
        heartbeat: Any | None = None,
    ) -> None:
        self.kernel_store = kernel_store
        self.orch = orch
        self.heartbeat = heartbeat
        self.worker_id = worker_id or f"goal-engine-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.wait_retry_seconds = wait_retry_seconds
        self.failover_wait_seconds = failover_wait_seconds

        self.router = router or ServiceRouter(orch)
        self.executors = executors or ServiceExecutors()
        handler = ServiceTaskHandler(self.router, self.executors, orch, kernel_store)
        self.executor = KernelExecutor(
            policy_engine=policy_engine,
            grant_store=grant_store,
            handler=handler,
        )
        self.dispatcher = KernelDispatcher(
            kernel_store,
            self.executor,
            worker_id=self.worker_id,
            lease_seconds=lease_seconds,
            poll_interval=poll_interval,
        )
        self.planner = planner or GoalPlanner(kernel_store, orch)
        self.judge = judge or GoalJudge(kernel_store, orch)
        self.wake = wake or WakeManager(orch)
        self._stop = asyncio.Event()

    # ── Goal lifecycle ──────────────────────────────────────────────────────

    async def _process_goal(self, goal: Goal) -> None:
        from antigona.observability_legacy import record as _legacy_record
        _legacy_record("orchestration.engine._process_goal")
        gid = goal.id
        flow = self.orch.get_current_flow(gid)
        # A goal needs a plan before anything else: PENDING (first cycle),
        # or ACTIVE resumed from WAIT/BLOCKED without a plan, or a
        # SUPERSEDED flow (replan in progress by another engine).
        if goal.status == GoalState.PENDING.value or flow is None or flow.status in (
            FlowState.SUPERSEDED.value,
            FlowState.PLANNED.value,
        ) or not (flow.plan or {}).get("task_ids"):
            try:
                if goal.status == GoalState.PENDING.value:
                    self.orch.transition_goal(gid, GoalState.PENDING, GoalState.ACTIVE)
                self.planner.plan(goal, flow_id="")
            except Exception as exc:  # noqa: BLE001
                logger.exception("planning failed for goal %s: %s", gid, exc)
                fresh = self.orch.get_goal(gid)
                if fresh is not None:
                    self.orch.transition_goal(
                        gid,
                        GoalState(fresh.status),
                        GoalState.FAILED,
                        failure_reason=f"planning failed: {exc}",
                    )
            return

        flow = self.orch.get_current_flow(gid)
        self._collect_verification_evidence(goal, flow)
        verdict = self.judge.decide(goal, flow)

        if verdict.decision == JudgeDecision.DONE:
            self.orch.transition_goal(
                gid, GoalState(goal.status), GoalState.SUCCEEDED,
                reason=verdict.reason, result={"evidence": verdict.evidence},
            )
            logger.info("goal %s DONE: %s", gid, verdict.reason)
        elif verdict.decision == JudgeDecision.FAIL:
            self.orch.transition_goal(
                gid, GoalState(goal.status), GoalState.FAILED,
                reason=verdict.reason, failure_reason=verdict.reason,
            )
        elif verdict.decision == JudgeDecision.WAIT:
            if goal.status != GoalState.WAITING.value:
                from datetime import UTC, datetime, timedelta

                wake_at = (datetime.now(UTC) + timedelta(seconds=self.wait_retry_seconds)).isoformat()
                # Timer-only wait: no wake event enqueued, so the goal stays
                # WAITING for the full duration (released worker, no tokens).
                self.wake.wait(gid, reason=verdict.reason, wake_at_iso=wake_at)
        elif verdict.decision == JudgeDecision.REPLAN:
            await self._replan(goal)
        elif verdict.decision == JudgeDecision.BLOCKED:
            self.judge.stall(goal)
            if (goal.meta or {}).get("stall_count", 0) > 3:
                self.orch.transition_goal(
                    gid, GoalState(goal.status), GoalState.BLOCKED,
                    reason="stalled with no progress",
                )
        # CONTINUE: nothing to do — the dispatcher picks up READY tasks.

    def _collect_verification_evidence(self, goal: Goal, flow: GoalFlow | None) -> bool:
        """When the CURRENT flow's verification task SUCCEEDED, durably record
        the evidence (flow-scoped) and mark the flow SUCCEEDED — the judge's
        DONE proof. Stale evidence from a superseded cycle is ignored (the
        new flow must produce its own evidence)."""
        if flow is None:
            return False
        meta = dict(goal.meta or {})
        existing = meta.get("verification_evidence") or {}
        if isinstance(existing, dict) and existing.get("flow_id") == flow.id:
            if flow.status != FlowState.SUCCEEDED.value:
                # Crash window: evidence was persisted but the flow CAS to
                # SUCCEEDED never landed. Re-assert with the fresh revision
                # (Grok audit Concurrency MAJOR — never a permanent CONTINUE).
                fresh = self.orch.get_flow(flow.id)
                if fresh is not None:
                    try:
                        self.orch.update_flow(
                            fresh.id,
                            expected_revision=fresh.revision,
                            status=FlowState.SUCCEEDED,
                        )
                    except Exception:  # noqa: BLE001 — someone else advanced
                        logger.warning("flow %s re-assert raced", flow.id)
            return True  # current flow already evidenced
        for tid in (flow.plan or {}).get("task_ids") or []:
            task = self.kernel_store.get_task(str(tid))
            if task is None:
                continue
            if str((task.payload or {}).get("stage") or "") != "verification":
                continue
            if task.status != TaskState.SUCCEEDED.value:
                return False
            self.orch.update_meta(
                goal.id,
                verification_evidence={
                    "flow_id": flow.id,
                    "task_id": task.id,
                    **dict((task.result or {}).get("evidence") or {}),
                    "verifier_service": (task.result or {}).get("service", ""),
                },
            )
            try:
                self.orch.update_flow(
                    flow.id,
                    expected_revision=flow.revision,
                    status=FlowState.SUCCEEDED,
                )
            except Exception:  # noqa: BLE001 — CAS conflict; someone else advanced
                logger.warning("flow %s already advanced (evidence race)", flow.id)
            return True
        return False

    async def _replan(self, goal: Goal) -> None:
        """REPLAN: supersede the current flow (history preserved), bump the
        cycle, create a new flow + new M1 tasks."""
        gid = goal.id
        self.orch.bump_cycle(gid)
        fresh = self.orch.get_goal(gid)
        if fresh is None:  # pragma: no cover — invariant
            return
        if (fresh.cycle_count or 0) >= (fresh.max_cycles or 1):
            self.orch.transition_goal(
                gid, GoalState(goal.status), GoalState.FAILED,
                reason="max cycles reached", failure_reason="max cycles reached",
            )
            return
        flow = self.orch.get_current_flow(gid)
        if flow is not None:
            try:
                # Cancel the old cycle's still-pending tasks so superseded work
                # stops executing (results of a dead cycle are never used).
                for tid in (flow.plan or {}).get("task_ids") or []:
                    task = self.kernel_store.get_task(str(tid))
                    if task is not None and task.status in (
                        TaskState.PENDING.value,
                        TaskState.READY.value,
                        TaskState.BLOCKED.value,
                    ):
                        try:
                            self.kernel_store.request_cancel(str(tid))
                        except Exception:  # noqa: BLE001
                            logger.warning("cancel of stale task %s failed", tid)
                self.orch.supersede_flow(flow.id, flow.revision)
            except Exception:  # noqa: BLE001 — conflict means someone else advanced
                logger.warning("supersede conflict for flow %s (another replan?)", flow.id)
        self.planner.plan(fresh, flow_id="")  # fresh cycle_count -> new task keys
        logger.info("goal %s REPLAN cycle %d", gid, fresh.cycle_count)

    # ── Main loop ───────────────────────────────────────────────────────────

    async def tick(self) -> int:
        """One orchestration pass. Returns number of goals processed."""
        # 1. M1 crash recovery.
        try:
            self.kernel_store.reconcile(
                worker_id=self.worker_id, lease_seconds=self.lease_seconds
            )
        except Exception:  # noqa: BLE001
            logger.exception("kernel reconcile failed")
        # 2. Durable wake processing (WAIT -> ACTIVE, timers).
        try:
            self.wake.process_pending()
        except Exception:  # noqa: BLE001
            logger.exception("wake processing failed")
        # 3. Judge + advance goals.
        processed = 0
        for goal in self.orch.list_active_goals():
            try:
                await self._process_goal(goal)
            except Exception:  # noqa: BLE001 — one goal must not kill the loop
                logger.exception("goal processing failed: %s", goal.id)
            processed += 1
        # 4. Execute READY kernel tasks through the service pool.
        try:
            await self.dispatcher.tick()
        except Exception:  # noqa: BLE001
            logger.exception("kernel dispatcher tick failed")
        return processed

    async def run_forever(self) -> None:
        logger.info("GoalEngine %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                if self.heartbeat is not None:
                    self.heartbeat.stamp_progress()
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("goal engine tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()