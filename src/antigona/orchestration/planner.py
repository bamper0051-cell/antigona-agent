"""Goal planner (M2): turns a durable Goal into an M1 Task DAG.

Deterministic, idempotent: the same (goal, cycle) triple always produces the
same idempotency keys, so a replay after a crash reuses the existing M1 tasks
instead of spawning duplicates; advancing to the next REPLAN cycle yields new
keys (the ``:c<N>`` cycle suffix — adopted from Claude Code's design during
integration).

The planner creates one M1 KernelTask per stage (analysis / implementation /
verification) via the M1 Durable Kernel store, then records the plan into the
Flow with optimistic-revision CAS (retrying once on FlowConflictError).
"""
from __future__ import annotations

import logging
from typing import Any

from antigona.kernel import KernelStore

from .models import Goal
from .state import FlowState
from .store import OrchestrationStore

logger = logging.getLogger(__name__)

STAGE_KEYS = ("analysis", "implementation", "verification")


class PlanResult:
    def __init__(self, flow_id: str, task_ids: list[str], stages: list[str]) -> None:
        self.flow_id = flow_id
        self.task_ids = task_ids
        self.stages = stages


def default_acceptance_criteria(objective: str) -> list[str]:
    del objective  # deterministic defaults; objective-specific criteria come from the owner
    return [
        "tasks executed successfully",
        "verification evidence recorded",
        "goal result persisted",
    ]


class GoalPlanner:
    def __init__(self, kernel_store: KernelStore, orch: OrchestrationStore) -> None:
        self.kernel_store = kernel_store
        self.orch = orch

    def _idempotency_key(self, goal: Goal, stage: str) -> str:
        cycle = int(goal.cycle_count or 0)
        return f"goal:{goal.id}:{stage}:c{cycle}"

    def plan(self, goal: Goal, flow_id: str) -> PlanResult:
        """Create a durable Flow (if flow_id empty) + one M1 task per stage.
        Idempotent on (goal, cycle, stage).

        A partially planned flow (PLANNED status, e.g. after a crash between
        create_flow and the last create_task) is resumed instead of orphaned.
        """
        if not goal.workspace:
            raise ValueError("structured workspace is required for autonomous goals")
        if not flow_id:
            existing = self.orch.get_flow(goal.current_flow_id) if goal.current_flow_id else None
            if existing is not None and existing.status == FlowState.PLANNED.value:
                # Crash recovery: resume the half-planned flow (Grok audit
                # Crash/Recovery MAJOR: no orphaned PLANNED flows).
                flow_id = existing.id
            else:
                flow = self.orch.create_flow(
                    goal.id,
                    plan={
                        "stages": list(STAGE_KEYS),
                        "task_ids": [],
                        "workspace": goal.workspace,
                        "mutation_required": bool(goal.mutation_required),
                    },
                    state={"goal_id": goal.id, "workspace": goal.workspace},
                    current_stage=STAGE_KEYS[0],
                )
                flow_id = flow.id
                self.orch.bind_flow(goal.id, flow_id)

        task_ids: list[str] = []
        prev_task_id: str | None = None  # stage DAG: analysis → impl → verification
        for stage in STAGE_KEYS:
            payload: dict[str, Any] = {
                "stage": stage,
                "objective": goal.objective,
                "goal_id": goal.id,
                "flow_id": flow_id,
                "workspace": goal.workspace,
                "mutation_required": bool(goal.mutation_required),
                "test_command": list(goal.test_command or []),
                "size": str((goal.meta or {}).get("size") or "medium"),
                # Role-scoped execution: verification must run on an
                # INDEPENDENT service (implementer != verifier, E2E-6 at
                # runtime, not just map-level).
                "role": "verifier" if stage == "verification" else "implementer",
            }
            if stage == "verification":
                payload["acceptance_criteria"] = list(goal.acceptance_criteria or [])
            # Hard stage ordering through the M1 kernel dependency graph: the
            # next stage is BLOCKED until the previous one SUCCEEDED, so
            # verification can never overtake implementation (Grok round-2
            # MAJOR: independence must be race-safe).
            dependencies = [prev_task_id] if prev_task_id else None
            task = self.kernel_store.create_task(
                owner_id=goal.owner_id,
                kind="goal_task",
                payload=payload,
                idempotency_key=self._idempotency_key(goal, stage),
                max_attempts=3,
                retryable=True,
                dependencies=dependencies,
            )
            task_ids.append(task.id)
            prev_task_id = task.id

        plan = {
            "stages": list(STAGE_KEYS),
            "task_ids": task_ids,
            "workspace": goal.workspace,
            "mutation_required": bool(goal.mutation_required),
        }
        stored_flow = self.orch.get_flow(flow_id)
        if stored_flow is None:  # pragma: no cover — invariant
            raise RuntimeError(f"flow vanished: {flow_id}")
        try:
            self.orch.update_flow(
                flow_id,
                expected_revision=stored_flow.revision,
                status=FlowState.ACTIVE,
                plan=plan,
                current_stage=STAGE_KEYS[0],
            )
        except Exception:  # noqa: BLE001 — CAS conflict: retry once with fresh revision
            fresh = self.orch.get_flow(flow_id)
            if fresh is None:
                raise
            if fresh.status == FlowState.SUPERSEDED.value:
                # Another engine already superseded this flow — never resurrect
                # a superseded flow back to ACTIVE (invariant).
                logger.warning("flow %s superseded; plan dropped", flow_id)
                return PlanResult(flow_id=flow_id, task_ids=task_ids, stages=list(STAGE_KEYS))
            self.orch.update_flow(
                flow_id,
                expected_revision=fresh.revision,
                status=FlowState.ACTIVE,
                plan=plan,
                current_stage=STAGE_KEYS[0],
            )
        return PlanResult(flow_id=flow_id, task_ids=task_ids, stages=list(STAGE_KEYS))

    def stage_for_task(self, task: Any) -> str:
        payload = getattr(task, "payload", None) or {}
        return str(payload.get("stage") or "")

    def plan_task_ids(self, goal: Goal, flow_id: str | None = None) -> list[str]:
        """Return the current flow's planned task ids (durable truth)."""
        fid = flow_id or goal.current_flow_id
        if not fid:
            return []
        flow = self.orch.get_flow(fid)
        if flow is None:
            return []
        return [str(t) for t in (flow.plan or {}).get("task_ids") or []]
