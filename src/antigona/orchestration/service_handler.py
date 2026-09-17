"""Autonomous Goal Orchestration (M2) — service task handler.

Bridges the M1 Durable Kernel to the multi-service pool: a kernel task
payload is routed to a service (Claude/Codex/agy/...), executed via the CLI
adapter, and on failure the handler FAILS OVER to the next suitable service,
recording a durable SERVICE_HANDOFF each time. If every service fails, the
handler raises a retryable error so the M1 kernel schedules a new Run (the
goal is never lost; the durable kernel owns retry/reconciliation).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from antigona.kernel.store import KernelStore
from antigona.orchestration.store import OrchestrationStore

from .autonomy import (
    AutonomyContractError,
    CandidateStore,
    FrozenCandidate,
    StageContext,
    StageEvaluation,
    WorkspaceBoundary,
    WorkspaceSnapshot,
    evaluate_stage,
    snapshot_workspace,
)
from .executors import ExecResult, ServiceExecutors
from .router import ServiceRouter

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


class AllServicesFailed(RuntimeError):
    """Raised (retryable) when no service in the ladder could execute the task."""


class ServiceTaskHandler:
    def __init__(
        self,
        router: ServiceRouter,
        executors: ServiceExecutors,
        orch: OrchestrationStore,
        kernel_store: KernelStore,
    ) -> None:
        self.router = router
        self.executors = executors
        self.orch = orch
        self.kernel_store = kernel_store

    async def __call__(
        self, payload: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """Route + execute + fail over. Returns structured result or raises
        AllServicesFailed (retryable by the M1 kernel)."""
        instruction = str(payload.get("instruction") or "").strip()
        if not instruction:
            stage = str(payload.get("stage") or "task")
            objective = str(payload.get("objective") or "").strip()
            instruction = f"{stage}: {objective}" if objective else f"execute goal stage: {stage}"
        size = str(payload.get("size") or "medium")
        role = str(payload.get("role") or "implementer")
        stage = str(payload.get("stage") or "task")
        goal_id = str(payload.get("goal_id") or "")
        avoid: set[str] = set()
        if role == "verifier" and goal_id:
            # Strict role separation (E2E-6 at runtime): the verifier must
            # NOT be the service that implemented this goal. The implementer
            # is recorded in goal.meta when the implementation stage succeeds.
            goal = self.orch.get_goal(goal_id)
            if goal is not None:
                impl = (goal.meta or {}).get("implementer_service")
                if impl:
                    avoid.add(str(impl))
        services_used: list[str] = []
        last_failure: str = ""
        task_id = str(context.get("task_id") or payload.get("task_id") or "")
        run_id = str(context.get("run_id") or "")
        flow_id = str(payload.get("flow_id") or "")
        workspace_text = str(payload.get("workspace") or "")
        if stage in {"analysis", "implementation", "verification"} and not workspace_text:
            raise AllServicesFailed("structured workspace is required")
        workspace = Path(workspace_text) if workspace_text else None
        if workspace is None:
            raise AllServicesFailed("structured workspace is required")
        goal_record = self.orch.get_goal(goal_id) if goal_id else None
        if goal_record is None or Path(goal_record.workspace).resolve() != workspace.resolve():
            raise AllServicesFailed("task workspace does not match its durable goal")
        self._validate_execution_binding(
            goal_id=goal_id,
            flow_id=flow_id,
            task_id=task_id,
            run_id=run_id,
            stage=stage,
        )
        mutation_required = bool(payload.get("mutation_required", False))
        before = snapshot_workspace(workspace)

        for attempt in range(4):  # bounded ladder walk
            service = self.router.route(
                payload, role=role, avoid=avoid, size=size
            )
            if service is None:
                break
            avoid.add(service)
            logger.info("task %s -> service %s (attempt %d)", task_id, service, attempt + 1)
            stage_instruction = self._stage_instruction(
                instruction, stage=stage, payload=payload, snapshot_hash=before.tree_hash if before else ""
            )
            evaluation: StageEvaluation | None = None
            try:
                result, evaluation = self._execute_stage(
                    service,
                    stage_instruction,
                    payload,
                    workspace=workspace,
                    before=before,
                    mutation_required=mutation_required,
                    goal_id=goal_id,
                    implementation_run_id=run_id,
                )
            except AutonomyContractError as exc:
                result = ExecResult(
                    ok=False,
                    output=str(exc),
                    failure_class="INVALID_OUTPUT",
                    duration_s=0.0,
                    service_id=service,
                )
            if result.ok:
                self.router.record_success(service)
                assert evaluation is not None
                evidence = dict(evaluation.evidence)
                if stage == "implementation":
                    evidence.update(
                        {
                            "flow_id": flow_id,
                            "goal_id": goal_id,
                            "producer_task_id": task_id,
                            "producer_run_id": run_id,
                            "producer_service": service,
                            "goal_hash": sha256(goal_record.objective.encode()).hexdigest(),
                            "produced_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    if not mutation_required:
                        evidence["workspace_hash"] = evidence.get("input_hash")
                elif stage == "verification":
                    evidence.update(
                        {
                            "flow_id": flow_id,
                            "goal_id": goal_id,
                            "verifier_task_id": task_id,
                            "verifier_run_id": run_id,
                            "verified_at": datetime.now(UTC).isoformat(),
                        }
                    )
                evaluation = StageEvaluation(evidence, evaluation.candidate)
                if stage == "implementation" and goal_id:
                    # Record who implemented, so verification can stay
                    # independent (never the same service).
                    assert evaluation is not None
                    if mutation_required:
                        self.orch.update_meta(
                            goal_id,
                            implementer_service=service,
                            implementation_evidence=evaluation.evidence,
                        )
                    else:
                        self.orch.update_meta(
                            goal_id,
                            implementer_service=service,
                            producer_evidence=evaluation.evidence,
                        )
                return {
                    "ok": True,
                    "service": service,
                    "output": result.output[:4000],
                    "evidence": evaluation.evidence if evaluation else {},
                    "services_used": services_used + [service],
                    "attempts": attempt + 1,
                }
            # Failure: classify + persist handoff, then fail over.
            self.router.record_failure(service, result.failure_class)
            last_failure = f"{service}:{result.failure_class}"
            logger.warning("service %s failed (%s): %s", service, result.failure_class, result.output[:300])
            # Next candidate (may be none — the ladder is exhausted; label it
            # honestly as "none", never as a service that did not run).
            replacement = self.router.route(payload, role=role, avoid=avoid, size=size) or "none"
            try:
                self.orch.create_handoff(
                    original_worker=service,
                    replacement_worker=replacement,
                    reason_for_handoff=f"{result.failure_class}: {result.output[:200]}",
                    goal_id=str(payload.get("goal_id") or ""),
                    flow_id=str(payload.get("flow_id") or ""),
                    task_id=task_id,
                    handoff_state={
                        "instruction": instruction[:500],
                        "services_used": services_used,
                        "last_failure": last_failure,
                        "task_stage": payload.get("stage", ""),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — handoff persistence must not mask the failure
                logger.warning("handoff record failed: %s", exc)
            services_used.append(service)

        raise AllServicesFailed(
            f"no service could execute the task; last failures: {last_failure or 'none'}"
        )

    def _validate_execution_binding(
        self,
        *,
        goal_id: str,
        flow_id: str,
        task_id: str,
        run_id: str,
        stage: str,
    ) -> None:
        """Bind service output to immutable current Flow/Task/Run identities."""
        if not all((goal_id, flow_id, task_id, run_id)):
            raise AllServicesFailed("goal flow task and run identity are required")
        goal = self.orch.get_goal(goal_id)
        flow = self.orch.get_flow(flow_id)
        task = self.kernel_store.get_task(task_id)
        run = self.kernel_store.get_run(run_id)
        if (
            goal is None
            or goal.current_flow_id != flow_id
            or flow is None
            or flow.goal_id != goal_id
            or task_id not in [str(item) for item in (flow.plan or {}).get("task_ids") or []]
            or task is None
            or str((task.payload or {}).get("flow_id") or "") != flow_id
            or str((task.payload or {}).get("goal_id") or "") != goal_id
            or str((task.payload or {}).get("stage") or "") != stage
            or run is None
            or run.task_id != task_id
        ):
            raise AllServicesFailed("service execution is not bound to the current flow")

    def _execute(
        self, service: str, instruction: str, payload: dict[str, Any], *,
        workspace: Path, writable: bool,
    ) -> ExecResult:
        # Tests inject a fake service; otherwise run the real CLI adapter.
        fake = payload.get("_fake_service")
        if fake and fake.get("service") == service:
            return self.executors.simulate(service, str(fake.get("failure_class") or ""))
        if payload.get("_always_simulate"):
            return self.executors.simulate(service, "RATE_LIMIT")
        return self.executors.execute(
            service, instruction, workspace=str(workspace), writable=writable
        )

    def _execute_stage(
        self,
        service: str,
        instruction: str,
        payload: dict[str, Any],
        *,
        workspace: Path | None,
        before: WorkspaceSnapshot,
        mutation_required: bool,
        goal_id: str,
        implementation_run_id: str,
    ) -> tuple[ExecResult, StageEvaluation | None]:
        if workspace is None:
            raise AutonomyContractError("workspace is required")
        stage = str(payload.get("stage") or "")
        if stage == "analysis":
            result = self._execute(service, instruction, payload, workspace=workspace, writable=False)
            if not result.ok:
                return result, None
            return result, evaluate_stage(StageContext.analysis(workspace, before.tree_hash), result.output)
        if stage == "implementation":
            result = self._execute(
                service, instruction, payload, workspace=workspace, writable=mutation_required
            )
            if not result.ok:
                return result, None
            if mutation_required:
                context = StageContext.implementation(
                    workspace, before, mutation_required=True,
                    candidate_store=workspace / ".antigona" / "candidates",
                    implementation_run_id=implementation_run_id,
                )
            else:
                context = StageContext.result_producer(workspace, before.tree_hash)
            return result, evaluate_stage(context, result.output)
        if stage == "verification":
            goal = self.orch.get_goal(goal_id)
            meta = dict(goal.meta or {}) if goal is not None else {}
            implementer = str(meta.get("implementer_service") or "")
            if mutation_required:
                raw_candidate = meta.get("implementation_evidence")
                if not isinstance(raw_candidate, dict):
                    raise AutonomyContractError("candidate provenance is missing")
                candidate = FrozenCandidate.from_dict(raw_candidate)
                test_command = [str(item) for item in payload.get("test_command") or []]
                criteria = [str(item) for item in payload.get("acceptance_criteria") or []]
                if test_command:
                    # Variant A: deterministic verifier — run the test on the frozen
                    # candidate; exit 0 means every acceptance criterion passed. No LLM.
                    store = CandidateStore(Path(candidate.archive_path).parent)
                    with store.materialize(candidate) as copy:
                        test = WorkspaceBoundary(copy, writable=False).run(
                            test_command, timeout=120
                        )
                    rc = test.returncode
                    verif_payload = {
                        "kind": "verification",
                        "candidate_hash": candidate.candidate_hash,
                        "summary": f"deterministic verifier: test_command exit {rc}",
                        "criteria": [
                            {"criterion": c, "passed": rc == 0,
                             "evidence": f"test_command exited {rc}"}
                            for c in criteria
                        ],
                    }
                    result = ExecResult(
                        ok=rc == 0, output=json.dumps(verif_payload),
                        failure_class="", duration_s=0.0, service_id="deterministic",
                    )
                    if not result.ok:
                        return result, None
                    context = StageContext.verification(
                        workspace, candidate,
                        implementer_service=implementer,
                        verifier_service="deterministic",
                        test_command=test_command,
                        criteria=criteria,
                    )
                    return result, evaluate_stage(context, result.output)
                instruction = (
                    instruction.replace("the supplied candidate hash", candidate.candidate_hash)
                    + "\nFrozen candidate evidence: " + json.dumps(candidate.as_dict(), sort_keys=True)
                )
                store = CandidateStore(Path(candidate.archive_path).parent)
                with store.materialize(candidate) as copy:
                    result = self._execute(
                        service, instruction, payload, workspace=copy, writable=False
                    )
                if not result.ok:
                    return result, None
                context = StageContext.verification(
                    workspace,
                    candidate,
                    implementer_service=implementer,
                    verifier_service=service,
                    test_command=test_command,
                    criteria=criteria,
                )
                return result, evaluate_stage(context, result.output)
            else:
                producer = meta.get("producer_evidence")
                if not isinstance(producer, dict):
                    raise AutonomyContractError("result was first produced by verifier")
                instruction = (
                    instruction.replace(
                        "supplied hash", str(producer.get("result_hash") or "")
                    )
                    + "\nProducer evidence: " + json.dumps(producer, sort_keys=True)
                )
                result = self._execute(
                    service, instruction, payload, workspace=workspace, writable=False
                )
                if not result.ok:
                    return result, None
                context = StageContext.result_verifier(
                    workspace,
                    str(producer.get("input_hash") or ""),
                    producer_service=implementer,
                    verifier_service=service,
                    produced_result=producer,
                    criteria=[str(item) for item in payload.get("acceptance_criteria") or []],
                )
            return result, evaluate_stage(context, result.output)
        raise AutonomyContractError(f"unsupported goal stage: {stage}")

    @staticmethod
    def _stage_instruction(
        instruction: str, *, stage: str, payload: dict[str, Any], snapshot_hash: str
    ) -> str:
        workspace = str(payload.get("workspace") or "")
        if stage == "analysis":
            contract = (
                '{"kind":"analysis","snapshot_hash":"' + snapshot_hash
                + '","summary":"...","evidence":[{"path":"relative",'
                  '"sha256":"64 hex","line":1}]}'
            )
        elif stage == "implementation" and payload.get("mutation_required"):
            contract = '{"kind":"implementation","summary":"what changed"}'
        elif stage == "implementation":
            contract = (
                '{"kind":"result","input_hash":"' + snapshot_hash
                + '","result":{},"derivation":"how computed"}'
            )
        elif payload.get("mutation_required"):
            criteria = json.dumps(payload.get("acceptance_criteria") or [])
            contract = (
                '{"kind":"verification","candidate_hash":"the supplied candidate hash",'
                '"criteria":[{"criterion":"exact criterion","passed":true,'
                f'"evidence":"checked"}}]}}; criteria={criteria}'
            )
        else:
            criteria = json.dumps(payload.get("acceptance_criteria") or [])
            contract = (
                '{"kind":"result_verification","input_hash":"' + snapshot_hash
                + '","producer_result_hash":"supplied hash","passed":true,'
                  '"evidence":"independent recomputation","criteria":'
                + '[{"criterion":"exact criterion","passed":true,"evidence":"checked"}]}'
                + f"; criteria={criteria}"
            )
        stage_note = ""
        if stage == "analysis":
            stage_note = (
                " You are performing the ANALYSIS stage: DO NOT modify or create any "
                "files. Inspect the files CURRENTLY present in the workspace and cite at "
                "least one EXISTING file as evidence (relative path, sha256 of that file's "
                "current content, and a valid line number within it)."
            )
        elif stage in ("implementation", "verification", "result_verification"):
            stage_note = (
                " After performing the stage actions on the workspace, return the "
                "result contract below."
            )
        sandbox_workspace = "/tmp/workspace" if workspace else workspace
        return (
            f"{instruction}\nWorkspace: {sandbox_workspace}\nStage: {stage}.{stage_note}\n"
            "IMPORTANT: Return ONLY the raw JSON object matching the contract below. "
            "Do NOT wrap it in markdown code fences, do NOT add commentary, prose, or "
            "explanation before or after. The entire response must be a single JSON "
            "object and nothing else.\n"
            "Contract: " + contract
        )
