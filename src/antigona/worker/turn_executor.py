"""TurnTaskExecutor — run read-only TaskFlows through the TurnEngine.

The executor is the thin adapter between the durable task machinery
(:class:`TaskRepository`, the state machine, the Verifier) and the
:class:`~antigona.turn_bridge.worker_adapter.TurnWorker`, which drives the
full model → tool → result → model cycle over workspace-scoped read-only
tools.

It mirrors :class:`~antigona.orchestrator.Orchestrator` on the parts that the
rest of the system depends on:

* the same lease discipline (acquire, run, release),
* the same transition chain ``RECEIVED → QUEUED → PLANNING → TOOL_EXECUTING
  → OBSERVING → VERIFYING`` (the Verifier's trajectory check refuses gaps),
* the same terminal rule — ``VERIFYING → DONE`` belongs to the Verifier
  service alone; on a declined verification the executor only records
  ``FAILED`` when the Verifier did not already write an outcome itself.

The turn output is materialised as a real artifact file under the workspace so
that the Verifier — which re-reads and re-hashes artifact bytes from disk — can
reach a ``DONE`` decision. Artifacts are written to a dedicated
``turn_results/`` namespace, never to ``task.target_path``: a read-only flow
must not overwrite the very file it was asked to read.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Protocol

from antigona.durable.state_cache import StateCache
from antigona.models import Artifact, StepState, TaskFlow, TaskState
from antigona.ownership.epoch import OwnershipContext
from antigona.ownership.wiring import enforce_write_fence
from antigona.pipeline import CompletionVerifier
from antigona.repository import LeaseConflict, TaskRepository
from antigona.result_safety import is_sensitive_execution, is_usable_result_text
from antigona.turn_bridge.turn_engine_adapter import TurnBudget, TurnResult
from antigona.turn_bridge.worker_adapter import TurnWorker

LOGGER = logging.getLogger(__name__)

#: Workspace-relative directory holding materialised turn results.
RESULTS_DIRECTORY = "turn_results"


class TurnRuntime(Protocol):
    """Synchronous facade over an async TurnWorker."""

    def execute(
        self, flow_id: str, goal: str, messages: list[dict[str, Any]]
    ) -> TurnResult:
        """Run one read-only turn and return its result."""
        ...


def classify_turn_failure(result: TurnResult) -> str:
    """Map a failed :class:`TurnResult` to a fixed, non-diagnostic category.

    Provider error text is untrusted and must never reach transitions, the
    outbox, or public task state — only the category travels.
    """

    error = (result.error or "").lower()
    if "budget exceeded" in error:
        return "turn engine budget exhausted"
    if "no choices" in error or "no response" in error:
        return "turn engine returned no response"
    if error:
        return "turn engine provider error"
    return "turn engine produced no usable result"


class TurnWorkerRuntime:
    """Owns a :class:`TurnWorker` plus the event loop its HTTP client is bound to.

    The Gateway worker loop is synchronous while the TurnEngine is async, and
    the provider's ``httpx.AsyncClient`` binds its connection pool to the first
    loop that uses it.  A single long-lived loop, reused across every task, is
    therefore the only safe bridge — ``asyncio.run`` per task would leave the
    pool attached to a closed loop.

    P1-04: When initialized with ``settings``, the runtime refreshes its
    underlying :class:`TurnWorker` per turn from the canonical ProviderResolver
    state (persisted ``/setllm`` selection) so a mid-flight operator command
    takes effect on the very next turn without process restart, while keeping
    the active turn's execution snapshot stable.

    Args:
        worker: Optional initial :class:`TurnWorker`.
        budget: Optional budget override for every turn.
        settings: Optional :class:`Settings` to enable dynamic per-turn provider/model resolution.
        workspace_path: Optional workspace path for newly constructed workers.
        timeout_seconds: Optional provider timeout in seconds.
        max_retries: Optional provider max retries.
    """

    def __init__(
        self,
        worker: TurnWorker | None = None,
        budget: TurnBudget | None = None,
        *,
        settings: Any = None,
        workspace_path: str | None = None,
        timeout_seconds: int = 120,
        max_retries: int = 2,
    ) -> None:
        self._worker = worker
        self._budget = budget
        self._settings = settings
        self._workspace_path = workspace_path
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._current_config: tuple[str, str, str] | None = None
        if worker is not None:
            provider_cfg = worker.engine.provider.config
            self._current_config = (
                provider_cfg.base_url,
                provider_cfg.api_key,
                provider_cfg.model,
            )
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _sync_worker_for_turn(self) -> TurnWorker:
        """Resolve current provider config per-turn snapshot (P1-04)."""
        if self._settings is None:
            if self._worker is None:
                raise RuntimeError("TurnWorkerRuntime has no worker or settings configured")
            return self._worker

        from antigona.worker import _resolve_worker_llm_config

        base_url, api_key, model = _resolve_worker_llm_config(self._settings)
        if self._worker is None or self._current_config != (base_url, api_key, model):
            loop = self._ensure_loop()
            if self._worker is not None:
                try:
                    if loop.is_running():
                        asyncio.create_task(self._worker.close())
                    else:
                        loop.run_until_complete(self._worker.close())
                except Exception:
                    pass
            self._worker = TurnWorker(
                base_url=base_url,
                api_key=api_key,
                model=model,
                workspace_path=self._workspace_path or str(getattr(self._settings, "workspace", "")),
                timeout_seconds=self._timeout_seconds or getattr(self._settings, "llm_timeout_seconds", 120),
                max_retries=self._max_retries,
            )
            self._current_config = (base_url, api_key, model)
        return self._worker

    @property
    def worker(self) -> TurnWorker:
        return self._sync_worker_for_turn()

    def execute(
        self, flow_id: str, goal: str, messages: list[dict[str, Any]]
    ) -> TurnResult:
        worker = self._sync_worker_for_turn()
        loop = self._ensure_loop()
        return loop.run_until_complete(
            worker.execute_task(
                flow_id=flow_id,
                goal=goal,
                messages=messages,
                budget=self._budget,
            )
        )

    def close(self) -> None:
        """Close the provider client and the owned loop."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            if self._worker is not None:
                self._loop.run_until_complete(self._worker.close())
        finally:
            self._loop.close()
            self._loop = None


def build_turn_runtime(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    workspace_path: str | None = None,
    timeout_seconds: int = 120,
    max_retries: int = 2,
    budget: TurnBudget | None = None,
    settings: Any = None,
) -> TurnWorkerRuntime:
    """Build a :class:`TurnWorkerRuntime` from plain provider settings."""
    worker: TurnWorker | None = None
    if base_url is not None and api_key is not None and model is not None:
        worker = TurnWorker(
            base_url=base_url,
            api_key=api_key,
            model=model,
            workspace_path=workspace_path,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
    return TurnWorkerRuntime(
        worker=worker,
        budget=budget,
        settings=settings,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


class TurnTaskExecutor:
    """Executes a read-only :class:`TaskFlow` through the TurnEngine.

    Args:
        session: An open SQLAlchemy session.
        verifier: The completion verifier client.
        runtime: Synchronous facade over the TurnWorker.
        workspace_root: Root the artifact file is written under; must match the
            ``ANTIGONA_WORKSPACE`` the Verifier service reads from.
        lease_seconds: Lease duration for the task row.
        state_cache: Optional durable state cache (write-after-commit only).
    """

    def __init__(
        self,
        session: Any,
        verifier: CompletionVerifier,
        *,
        runtime: TurnRuntime,
        workspace_root: Path | str,
        lease_seconds: int = 30,
        state_cache: StateCache | None = None,
        ownership: OwnershipContext | None = None,
    ) -> None:
        self.repository = TaskRepository(session, state_cache)
        self.verifier = verifier
        self.runtime = runtime
        self.workspace_root = Path(workspace_root)
        self.lease_seconds = lease_seconds
        #: Live ownership fencing token (DF-WO2-003-residual); None when disabled
        #: or not forwarded.  Fenced at the ``_persist_artifact`` mutation
        #: boundary so a stale/unauthorised owner cannot write a turn result into
        #: the protected workspace root (fail-closed, INV-06).
        self.ownership = ownership

    # ── Public API ──────────────────────────────────────────────────────

    def run(self, task: TaskFlow, worker_id: str | None = None) -> TaskFlow:
        """Run *task* to a terminal (or verifier-owned) state and return it."""

        worker = worker_id or str(uuid.uuid4())
        correlation = str(uuid.uuid4())
        state = TaskState(task.status)
        if task.cancellation_requested or state in {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
            TaskState.TIMEOUT,
            TaskState.POLICY_DENIED,
        }:
            return task
        self.repository.acquire_lease(task, worker, self.lease_seconds)
        try:
            return self._resume(task, correlation)
        finally:
            try:
                self.repository.release_lease(task, worker)
            except LeaseConflict:
                pass

    # ── Flow ────────────────────────────────────────────────────────────

    def _resume(self, task: TaskFlow, correlation: str) -> TaskFlow:
        state = TaskState(task.status)
        if state is TaskState.RECEIVED:
            self.repository.transition(
                task, TaskState.QUEUED, "queued", "queue", correlation_id=correlation
            )
            self.repository.commit()
            state = TaskState.QUEUED
        if state is TaskState.QUEUED:
            self.repository.transition(
                task,
                TaskState.PLANNING,
                "read-only turn planned",
                "turn-executor",
                correlation_id=correlation,
            )
            task.checkpoint = "planned"
            self.repository.commit()
            state = TaskState.PLANNING
        if state is TaskState.PLANNING:
            approval = self.repository.request_approval(task)
            if approval.decision == "PENDING":
                self.repository.transition(
                    task,
                    TaskState.WAITING_APPROVAL,
                    f"{approval.risk_level}-risk tool requires approval: {approval.reason}",
                    "policy",
                    correlation_id=correlation,
                )
                task.checkpoint = "approval_requested"
                self.repository.commit()
                return self.repository.get(task.id)
            if approval.decision == "DENIED":
                self.repository.transition(
                    task,
                    TaskState.POLICY_DENIED,
                    "approval denied",
                    "policy",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return self.repository.get(task.id)
            self._dispatch(task, correlation)
            state = TaskState.TOOL_EXECUTING
        if state is TaskState.TOOL_EXECUTING:
            if self._block_sensitive_read(task, correlation):
                return self.repository.get(task.id)
            artifact = self._execute_turn(task, correlation)
            if artifact is None:
                return self.repository.get(task.id)
            self.repository.transition(
                task,
                TaskState.OBSERVING,
                "turn evidence persisted",
                "turn-executor",
                correlation_id=correlation,
            )
            task.checkpoint = "observed"
            self.repository.commit()
            state = TaskState.OBSERVING
        if state is TaskState.OBSERVING:
            self.repository.transition(
                task,
                TaskState.VERIFYING,
                "completion requested",
                "turn-executor",
                correlation_id=correlation,
            )
            task.checkpoint = "verifying"
            self.repository.commit()
            state = TaskState.VERIFYING
        if state is TaskState.VERIFYING:
            self._verify(task, correlation)
        return self.repository.get(task.id)

    def _dispatch(self, task: TaskFlow, correlation: str) -> None:
        step = task.steps[0]
        if step.status == StepState.PENDING.value:
            self.repository.transition_step(
                task,
                step,
                StepState.RUNNING,
                "read-only turn dispatched",
                "turn-executor",
                correlation,
            )
        task.side_effect_key = hashlib.sha256(
            f"{task.id}:{step.id}:{task.tool_name}".encode()
        ).hexdigest()
        task.checkpoint = "tool_dispatched"
        self.repository.transition(
            task,
            TaskState.TOOL_EXECUTING,
            "read-only turn dispatched",
            "turn-executor",
            correlation_id=correlation,
        )
        self.repository.commit()

    def _block_sensitive_read(self, task: TaskFlow, correlation: str) -> bool:
        """Refuse a read whose declared target is outside the safety policy."""

        if not is_sensitive_execution(
            task.target_path,
            (),
            content=task.content,
            workspace_root=self.workspace_root,
        ):
            return False
        step = task.steps[0]
        step.output = {
            "ok": False,
            "side_effect_key": f"{task.id}:{step.id}",
            "tool_result": {"ok": False, "error": "blocked by safety policy"},
        }
        if step.status == StepState.RUNNING.value:
            self.repository.transition_step(
                task,
                step,
                StepState.FAILED,
                "Execution blocked by result safety policy",
                "turn-executor",
                correlation,
            )
        self.repository.transition(
            task,
            TaskState.BLOCKED,
            "Execution blocked by result safety policy",
            "turn-executor",
            correlation_id=correlation,
        )
        self.repository.commit()
        return True

    # ── Turn execution ──────────────────────────────────────────────────

    def _execute_turn(self, task: TaskFlow, correlation: str) -> Artifact | None:
        """Run the turn and persist its result; ``None`` means the task failed."""

        if task.artifacts:
            return task.artifacts[0]

        result = self.runtime.execute(
            flow_id=task.id,
            goal=task.goal,
            messages=build_messages(task),
        )
        if not result.success or not is_usable_result_text(result.final_response):
            self._fail(task, correlation, classify_turn_failure(result))
            return None

        assert result.final_response is not None
        return self._persist_artifact(task, result, correlation)

    def _persist_artifact(
        self, task: TaskFlow, result: TurnResult, correlation: str
    ) -> Artifact:
        step = task.steps[0]
        relative_path = f"{RESULTS_DIRECTORY}/{task.id}.md"
        payload = str(result.final_response).encode("utf-8")
        target = self.workspace_root / relative_path
        # DF-WO2-003-residual: fence the mutation boundary BEFORE any filesystem
        # side effect.  Ownership DISABLED (default) -> no-op (backward-compat).
        # Ownership ENABLED + stale/unauthorised owner or no token -> raises
        # FenceDeniedError before ``mkdir``/``write_bytes`` so no file is created
        # (fail-closed, INV-06).
        enforce_write_fence(self.ownership, "turn_results")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

        output: dict[str, Any] = {
            "ok": True,
            "side_effect_key": task.side_effect_key,
            "tool_result": {
                "ok": True,
                "status": "completed",
                "turns_used": result.turns_used,
                "tool_calls_made": result.tool_calls_made,
            },
        }
        step.output = output
        self.repository.transition_step(
            task,
            step,
            StepState.COMPLETED,
            "read-only turn completed",
            "turn-executor",
            correlation,
        )
        artifact = Artifact(
            task_id=task.id,
            step_id=step.id,
            path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            evidence=output,
        )
        task.artifacts.append(artifact)
        task.checkpoint = "tool_completed"
        self.repository.session.flush()
        self.repository.commit()
        return artifact

    def _fail(self, task: TaskFlow, correlation: str, reason: str) -> None:
        step = task.steps[0]
        if step.status == StepState.RUNNING.value:
            self.repository.transition_step(
                task, step, StepState.FAILED, reason, "turn-executor", correlation
            )
        self.repository.transition(
            task, TaskState.FAILED, reason, "turn-executor", correlation_id=correlation
        )
        self.repository.commit()

    # ── Verification ────────────────────────────────────────────────────

    def _verify(self, task: TaskFlow, correlation: str) -> None:
        verifier_unavailable = False
        try:
            decision = self.verifier.request_verification(task.id, correlation)
        except Exception:
            # Client/provider exception detail is untrusted; only the fixed
            # reason below is allowed to reach durable state.
            decision = "REPLAN"
            verifier_unavailable = True
        if decision == "DONE":
            return
        reason = (
            "verification service unavailable"
            if verifier_unavailable
            else "verification requested replan"
        )
        # VERIFYING -> DONE is the Verifier service's own capability, and it
        # commits its outcome before answering. Re-read the authoritative row
        # and only record a failure when the Verifier declined without writing
        # one itself.
        current = self.repository.get(task.id)
        if TaskState(current.status) is TaskState.VERIFYING:
            self.repository.transition(
                current,
                TaskState.FAILED,
                reason,
                "verifier-client",
                correlation_id=correlation,
            )
            self.repository.commit()


def build_messages(task: TaskFlow) -> list[dict[str, Any]]:
    """Build the initial message history for a read-only task."""

    parts = [task.goal]
    if task.target_path:
        parts.append(f"Target path: {task.target_path}")
    if task.content:
        parts.append(f"Context content:\n{task.content}")
    return [{"role": "user", "content": "\n\n".join(parts)}]


__all__ = [
    "RESULTS_DIRECTORY",
    "TurnRuntime",
    "TurnTaskExecutor",
    "TurnWorkerRuntime",
    "build_messages",
    "build_turn_runtime",
    "classify_turn_failure",
]
