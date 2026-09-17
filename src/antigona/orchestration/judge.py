"""Autonomous Goal Orchestration (M2) — Goal Judge.

The judge answers one question per tick — *what should happen to this Goal
now?* — from **durable state only**: the Goal row, its Flow row, the M1 kernel
tasks named by the flow plan and the pending wake queue. No LLM call, no
network, no wall-clock heuristics: the same durable state always yields the
same verdict, so a restarted engine re-derives identical decisions.

DONE is evidence-based: a Goal only succeeds when the flow succeeded, every
planned task succeeded, acceptance criteria exist and verification evidence
was actually recorded on the goal.
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from antigona.kernel import KernelStore, RunState, TaskState

from .models import Goal, GoalFlow
from .state import FlowState, GoalState, JudgeDecision
from .store import OrchestrationStore

# Task states that still hold execution capacity (work may yet happen).
_PENDING_STATES = frozenset(
    {TaskState.PENDING.value, TaskState.READY.value, TaskState.BLOCKED.value}
)
_ACTIVE_STATES = _PENDING_STATES | {TaskState.RUNNING.value}

#: Outputs that must never count as verification evidence (stub/planner-only).
_STUB_SERVICES = frozenset({"hermes", "<hermes-internal>"})

_STALL_LIMIT = 2


class JudgeVerdict:
    def __init__(
        self,
        decision: JudgeDecision,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.evidence: dict[str, Any] = dict(evidence or {})

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"JudgeVerdict({self.decision.value}, {self.reason!r})"


class GoalJudge:
    def __init__(self, kernel_store: KernelStore, orch: OrchestrationStore) -> None:
        self.kernel_store = kernel_store
        self.orch = orch

    # ── decision ────────────────────────────────────────────────────────────

    def decide(self, goal: Goal, flow: GoalFlow | None) -> JudgeVerdict:
        meta: dict[str, Any] = dict(goal.meta or {})
        criteria = list(goal.acceptance_criteria or [])
        task_ids = self._plan_task_ids(flow)
        states = self._task_states(task_ids)

        succeeded = [tid for tid, st in states.items() if st == TaskState.SUCCEEDED.value]
        failed = [tid for tid, st in states.items() if st == TaskState.FAILED.value]
        cancelled = [tid for tid, st in states.items() if st == TaskState.CANCELLED.value]
        pending = [tid for tid, st in states.items() if st in _PENDING_STATES]
        active = [tid for tid, st in states.items() if st in _ACTIVE_STATES]

        evidence: dict[str, Any] = {
            "flow_id": flow.id if flow is not None else "",
            "flow_status": flow.status if flow is not None else "",
            "tasks_planned": len(task_ids),
            "tasks_succeeded": len(succeeded),
            "tasks_failed": len(failed),
            "tasks_cancelled": len(cancelled),
            "tasks_pending": len(pending),
            "acceptance_criteria": criteria,
            "cycle_count": int(goal.cycle_count or 0),
            "max_cycles": int(goal.max_cycles or 0),
            "stall_count": int(meta.get("stall_count") or 0),
        }

        # 1. DONE — every durable proof present: flow SUCCEEDED, all plan
        # tasks SUCCEEDED, acceptance criteria, and VERIFICATION EVIDENCE that
        # is scoped to THIS flow and produced by a real (non-stub) service.
        verification_evidence = meta.get("verification_evidence")
        if not isinstance(verification_evidence, dict):
            verification_evidence = {}
        ev_service = str(verification_evidence.get("verifier_service") or "")
        impl_service = str(meta.get("implementer_service") or "")
        if bool(goal.mutation_required):
            implementation = meta.get("implementation_evidence")
            evidence_ok = bool(
                isinstance(implementation, dict)
                and verification_evidence.get("flow_id") == (flow.id if flow else None)
                and implementation.get("flow_id") == (flow.id if flow else None)
                and verification_evidence.get("workspace") == goal.workspace
                and implementation.get("workspace") == goal.workspace
                and implementation.get("candidate_hash")
                == verification_evidence.get("candidate_hash")
                == verification_evidence.get("tested_candidate_hash")
                and implementation.get("baseline_hash")
                == verification_evidence.get("baseline_hash")
                and implementation.get("source_diff_hash")
                == verification_evidence.get("source_diff_hash")
                and implementation.get("protected_manifest_hash")
                == verification_evidence.get("protected_manifest_hash")
                and bool(implementation.get("implementation_run_id"))
                and implementation.get("implementation_run_id")
                == verification_evidence.get("implementation_run_id")
                and bool(implementation.get("candidate_timestamp"))
                and implementation.get("candidate_timestamp")
                == verification_evidence.get("candidate_timestamp")
                and verification_evidence.get("test_exit_code") == 0
                and verification_evidence.get("test_command") == list(goal.test_command or [])
                and verification_evidence.get("read_only_boundary") == "bubblewrap"
                and self._criteria_pass(criteria, verification_evidence.get("criteria"))
                and ev_service not in _STUB_SERVICES
                and impl_service
                and ev_service != impl_service
            )
        else:
            producer = meta.get("producer_evidence")
            evidence_ok = bool(
                isinstance(producer, dict)
                and flow is not None
                and goal.current_flow_id == flow.id
                and producer.get("flow_id") == flow.id
                and producer.get("goal_id") == goal.id
                and producer.get("goal_hash") == sha256(goal.objective.encode()).hexdigest()
                and verification_evidence.get("flow_id") == (flow.id if flow else None)
                and verification_evidence.get("goal_id") == goal.id
                and verification_evidence.get("workspace") == goal.workspace
                and producer.get("workspace") == goal.workspace
                and producer.get("workspace_hash") == producer.get("input_hash")
                and producer.get("input_hash") == verification_evidence.get("input_hash")
                and producer.get("result_hash")
                == verification_evidence.get("producer_result_hash")
                and producer.get("producer_service") == impl_service
                and self._result_evidence_identity_valid(
                    producer, verification_evidence, goal, flow
                )
                and verification_evidence.get("passed") is True
                and str(verification_evidence.get("evidence") or "").strip()
                and self._criteria_pass(criteria, verification_evidence.get("criteria"))
                and ev_service not in _STUB_SERVICES
                and impl_service
                and ev_service != impl_service
            )
        all_succeeded = bool(task_ids) and len(succeeded) == len(task_ids)
        if (
            flow is not None
            and flow.status == FlowState.SUCCEEDED.value
            and all_succeeded
            and criteria
            and evidence_ok
        ):
            evidence["verification_evidence"] = verification_evidence
            return JudgeVerdict(
                JudgeDecision.DONE,
                "flow succeeded, all planned tasks succeeded, verification evidence recorded",
                evidence,
            )

        # 2. FAIL — a task failed and there is no retry capacity left.
        if (
            int(goal.cycle_count or 0) >= int(goal.max_cycles or 0)
            and failed
            and not active
        ):
            return JudgeVerdict(
                JudgeDecision.FAIL,
                f"{len(failed)} task(s) failed with no retry capacity "
                f"(cycle {goal.cycle_count}/{goal.max_cycles})",
                evidence,
            )

        # 3. No plan at all — the goal is not making progress on its own.
        if not task_ids:
            stalls = int(meta.get("stall_count") or 0)
            reason = (
                "no flow plan recorded and no progress"
                if stalls > _STALL_LIMIT
                else "no flow plan recorded yet"
            )
            return JudgeVerdict(JudgeDecision.BLOCKED, reason, evidence)

        # 4. WAIT — durable wake pending, or the goal already parked.
        if goal.status == GoalState.WAITING.value:
            return JudgeVerdict(JudgeDecision.WAIT, "goal parked on a durable wake", evidence)
        if self.orch.has_pending_wake_for(goal.id):
            return JudgeVerdict(
                JudgeDecision.WAIT, "pending wake event for this goal", evidence
            )

        # 5. REPLAN — a task failed but cycles remain.
        if failed and int(goal.cycle_count or 0) < int(goal.max_cycles or 0):
            return JudgeVerdict(
                JudgeDecision.REPLAN,
                f"{len(failed)} task(s) failed, replanning cycle "
                f"{int(goal.cycle_count or 0) + 1}/{goal.max_cycles}",
                evidence,
            )
        if failed:
            return JudgeVerdict(
                JudgeDecision.FAIL, "task failed and cycle budget exhausted", evidence
            )

        # 6. CONTINUE — visible progress with obvious remaining work.
        if succeeded and pending:
            return JudgeVerdict(
                JudgeDecision.CONTINUE,
                f"{len(succeeded)}/{len(task_ids)} tasks succeeded, {len(pending)} queued",
                evidence,
            )
        if succeeded and self._verification_incomplete(task_ids, states):
            return JudgeVerdict(
                JudgeDecision.CONTINUE, "verification stage not complete yet", evidence
            )

        # 7. CONTINUE — the dispatcher is working on the plan. Internal work
        # must NEVER be parked (WAIT is reserved for external conditions:
        # timer / owner / wake events / service availability).
        if pending:
            return JudgeVerdict(
                JudgeDecision.CONTINUE,
                f"{len(pending)} task(s) queued for the dispatcher",
                evidence,
            )
        if active:
            return JudgeVerdict(
                JudgeDecision.CONTINUE, f"{len(active)} task(s) running", evidence
            )

        # 8. Everything terminal but the DONE proof is missing.
        if all_succeeded:
            missing = []
            if flow is None or flow.status != FlowState.SUCCEEDED.value:
                missing.append("flow not marked SUCCEEDED")
            if not criteria:
                missing.append("no acceptance criteria")
            if not verification_evidence:
                missing.append("no verification evidence")
            return JudgeVerdict(
                JudgeDecision.CONTINUE,
                "tasks succeeded but DONE proof incomplete: " + ", ".join(missing),
                evidence,
            )

        return JudgeVerdict(
            JudgeDecision.BLOCKED, "no runnable task and no completion evidence", evidence
        )

    @staticmethod
    def _as_utc(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _result_evidence_identity_valid(
        self,
        producer: dict[str, Any],
        verification: dict[str, Any],
        goal: Goal,
        flow: GoalFlow,
    ) -> bool:
        producer_task_id = str(producer.get("producer_task_id") or "")
        producer_run_id = str(producer.get("producer_run_id") or "")
        verifier_task_id = str(verification.get("verifier_task_id") or "")
        verifier_run_id = str(verification.get("verifier_run_id") or "")
        task_ids = self._plan_task_ids(flow)
        producer_task = self.kernel_store.get_task(producer_task_id)
        verifier_task = self.kernel_store.get_task(verifier_task_id)
        producer_run = self.kernel_store.get_run(producer_run_id)
        verifier_run = self.kernel_store.get_run(verifier_run_id)
        if (
            producer_task_id not in task_ids
            or verifier_task_id not in task_ids
            or producer_task is None
            or verifier_task is None
            or producer_run is None
            or verifier_run is None
            or producer_task.status != TaskState.SUCCEEDED.value
            or verifier_task.status != TaskState.SUCCEEDED.value
            or producer_run.status != RunState.SUCCEEDED.value
            or verifier_run.status != RunState.SUCCEEDED.value
            or producer_run.task_id != producer_task_id
            or verifier_run.task_id != verifier_task_id
        ):
            return False
        producer_payload = dict(producer_task.payload or {})
        verifier_payload = dict(verifier_task.payload or {})
        raw_verification = (verifier_run.result or {}).get("evidence")
        if (
            producer_payload.get("stage") != "implementation"
            or verifier_payload.get("stage") != "verification"
            or producer_payload.get("goal_id") != goal.id
            or verifier_payload.get("goal_id") != goal.id
            or producer_payload.get("flow_id") != flow.id
            or verifier_payload.get("flow_id") != flow.id
            or (producer_run.result or {}).get("evidence") != producer
            or (producer_run.result or {}).get("service") != producer.get("producer_service")
            or not isinstance(raw_verification, dict)
            or any(verification.get(key) != value for key, value in raw_verification.items())
            or (verifier_run.result or {}).get("service")
            != verification.get("verifier_service")
        ):
            return False
        produced_at = self._as_utc(producer.get("produced_at"))
        verified_at = self._as_utc(verification.get("verified_at"))
        flow_created = flow.created_at.replace(tzinfo=UTC) if flow.created_at.tzinfo is None else flow.created_at
        producer_started = producer_run.started_at
        producer_finished = producer_run.finished_at
        verifier_started = verifier_run.started_at
        verifier_finished = verifier_run.finished_at
        if not all(
            (
                produced_at,
                verified_at,
                producer_started,
                producer_finished,
                verifier_started,
                verifier_finished,
            )
        ):
            return False
        assert produced_at is not None and verified_at is not None
        bounds = [producer_started, producer_finished, verifier_started, verifier_finished]
        normalized = [
            value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
            for value in bounds
        ]
        p_started, p_finished, v_started, v_finished = normalized
        assert p_started is not None and p_finished is not None
        assert v_started is not None and v_finished is not None
        return bool(
            flow_created <= p_started <= produced_at <= p_finished
            and produced_at <= v_started <= verified_at <= v_finished
        )

    def stall(self, goal: Goal) -> None:
        """Record one no-progress observation on the goal."""
        stalls = int((goal.meta or {}).get("stall_count") or 0) + 1
        self.orch.update_meta(goal.id, stall_count=stalls)

    # ── internals ───────────────────────────────────────────────────────────

    def _plan_task_ids(self, flow: GoalFlow | None) -> list[str]:
        if flow is None:
            return []
        plan = flow.plan or {}
        raw = plan.get("task_ids") or []
        if not isinstance(raw, list):
            return []
        return [str(tid) for tid in raw if tid]

    def _task_states(self, task_ids: list[str]) -> dict[str, str]:
        states: dict[str, str] = {}
        for tid in task_ids:
            task = self.kernel_store.get_task(tid)
            states[tid] = task.status if task is not None else TaskState.PENDING.value
        return states

    def _verification_incomplete(self, task_ids: list[str], states: dict[str, str]) -> bool:
        for tid in task_ids:
            task = self.kernel_store.get_task(tid)
            if task is None:
                continue
            if str((task.payload or {}).get("stage") or "") != "verification":
                continue
            return states.get(tid) != TaskState.SUCCEEDED.value
        return False

    @staticmethod
    def _criteria_pass(criteria: list[str], raw: object) -> bool:
        if not isinstance(raw, list) or len(raw) != len(criteria):
            return False
        evaluations = {
            str(item.get("criterion")): item for item in raw if isinstance(item, dict)
        }
        return all(
            name in evaluations
            and evaluations[name].get("passed") is True
            and bool(str(evaluations[name].get("evidence") or "").strip())
            for name in criteria
        )
