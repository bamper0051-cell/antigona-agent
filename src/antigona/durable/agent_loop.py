"""Agent Loop — autonomous task execution cycle for Antigona.

The :class:`AutonomousLoop` orchestrates the full plan → execute → observe →
verify cycle, with budget enforcement and error-fingerprint deduplication.

Lifecycle
---------
    CREATED → QUEUED → PLANNING → READY → EXECUTING → OBSERVING → VERIFYING
                ↑                    ↑               ⇣⇣⇣⇣⇣⇣⇣⇣⇣⇣⇣⇣⇣
            REPLAN_REQUESTED    RETRY_SCHEDULED  DONE / RETRY_SCHEDULED
                ↑                    ↑         REPLAN_REQUESTED / BLOCKED
            WAITING_USER       WAITING_APPROVAL WAITING_USER / WAITING_APPROVAL
                                                       FAILED

Ownership
---------
This is the **only** loop that progresses a task through the state machine.
No other component may call ``.transition()``
on a task's behalf — all state changes go through this loop.

Budget constraints
------------------
* ``max_attempts_per_step``  — hard limit on retries of one step (3)
* ``max_replans_per_task``   — hard limit on replan cycles (5)
* ``max_total_tool_calls``   — absolute tool-invocation budget (20)
* Error fingerprints prevent wasted retries on repeated errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from antigona.durable.execution_models import (
    ExecutionPlan,
    Observation,
    PlanStep,
    ToolExecutionResult,
    VerificationVerdict,
)
from antigona.durable.observer import Observer
from antigona.durable.planner import Planner
from antigona.durable.state_machine import InvalidTransition as StateMachineInvalidTransition
from antigona.durable.state_machine import check_task_transition
from antigona.durable.verifier import Verifier
from antigona.events.bus import EventBus
from antigona.events.event_types import (
    StageChanged,
    TaskCompleted,
    ToolProgress,
)
from antigona.models import TaskState

logger = logging.getLogger(__name__)

# ── Budget constants ─────────────────────────────────────────────────────────

MAX_ATTEMPTS_PER_STEP: int = 3
MAX_REPLANS_PER_TASK: int = 5
MAX_TOTAL_TOOL_CALLS: int = 20

# ── Typed alias ──────────────────────────────────────────────────────────────


class StepExecutor(Protocol):
    """Protocol for executing a single :class:`PlanStep`.

    Any callable that accepts a step and returns a
    :class:`ToolExecutionResult` satisfies this protocol.
    """

    async def __call__(self, step: PlanStep) -> ToolExecutionResult:
        ...


# ── Error fingerprint ────────────────────────────────────────────────────────


def _compute_error_fingerprint(
    result: ToolExecutionResult,
) -> str:
    """Produce a deterministic fingerprint for *result*.

    The fingerprint is a SHA-256 hex digest of::

        sha256(tool_name || \\0 || json(args) || \\0 || exit_code || \\0 || stderr)

    An empty fingerprint is returned when there is no error signal to
    fingerprint (exit_code == 0 and no stderr).
    """
    if result.exit_code == 0 and not result.stderr:
        return ""
    import json as _json

    args_json = _json.dumps(result.arguments, sort_keys=True, default=str)
    raw = (
        f"{result.tool_name}\0{args_json}\0"
        f"{result.exit_code or 0}\0{result.stderr or ''}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Loop result ──────────────────────────────────────────────────────────────


@dataclass
class LoopResult:
    """Outcome of an :meth:`AutonomousLoop.run` invocation.

    Attributes:
        success: *True* when the task reached ``DONE``.
        final_state: The terminal :class:`TaskState` reached.
        plan: The final :class:`ExecutionPlan` (useful for inspecting steps).
        iterations: How many steps were executed.
        tool_calls: Total tool calls made across all steps and replans.
        replans: How many replan cycles occurred.
        error: Human-readable error description, if any.
        verdict: The final :class:`VerificationVerdict`, if available.
    """

    success: bool = False
    final_state: TaskState = TaskState.FAILED
    plan: ExecutionPlan | None = None
    iterations: int = 0
    tool_calls: int = 0
    replans: int = 0
    error: str = ""
    verdict: VerificationVerdict | None = None


# ── Autonomous Loop ──────────────────────────────────────────────────────────


class AutonomousLoop:
    """Autonomous execution loop for Antigona tasks.

    Walks a task through the full lifecycle::

        plan → [execute → observe → verify → retry/replan/done]

    with hard budgets on attempts, replans, and total tool calls, and
    error-fingerprint deduplication that forces REPLAN instead of
    dead-end RETRY cycles.

    Parameters
    ----------
    planner:
        :class:`Planner` instance for creating and revising plans.
    observer:
        :class:`Observer` instance for analysing execution results.
    verifier:
        :class:`Verifier` instance for rendering verdicts.
    event_bus:
        :class:`EventBus` for publishing lifecycle events.
    executor:
        Callable that runs a :class:`PlanStep` and returns a
        :class:`ToolExecutionResult`.  When *None*, a default
        shell-based executor is used.
    max_attempts_per_step:
        Maximum retries for a single step (default 3).
    max_replans_per_task:
        Maximum replan cycles for a single task (default 5).
    max_total_tool_calls:
        Absolute tool-invocation budget (default 20).
    """

    def __init__(
        self,
        planner: Planner,
        observer: Observer,
        verifier: Verifier,
        event_bus: EventBus,
        executor: StepExecutor | None = None,
        *,
        max_attempts_per_step: int = MAX_ATTEMPTS_PER_STEP,
        max_replans_per_task: int = MAX_REPLANS_PER_TASK,
        max_total_tool_calls: int = MAX_TOTAL_TOOL_CALLS,
    ) -> None:
        self._planner = planner
        self._observer = observer
        self._verifier = verifier
        self._event_bus = event_bus
        self._executor = executor or _default_executor

        # Budgets
        self._max_attempts = max_attempts_per_step
        self._max_replans = max_replans_per_task
        self._max_tool_calls = max_total_tool_calls

        # Per-task mutable state (reset on each run())
        self._task_id: str = ""
        self._goal: str = ""
        self._context: dict[str, Any] = {}
        self._state: TaskState = TaskState.CREATED
        self._plan: ExecutionPlan | None = None
        self._current_step_index: int = 0

        # Budget counters
        self._tool_calls: int = 0
        self._replans: int = 0
        self._attempts: dict[str, int] = {}  # step_id → attempt_count

        # Error fingerprint → times_seen (per step_id)
        self._fingerprints: dict[str, dict[str, int]] = {}

        # Observations accumulated per step (for repeating-fingerprint detection)
        self._step_observations: dict[str, list[Observation]] = {}
        self._step_results: dict[str, list[ToolExecutionResult]] = {}

        self._cancel_event: asyncio.Event | None = None

        # The final verdict when the loop terminates
        self._final_verdict: VerificationVerdict | None = None

    # ── Public API ───────────────────────────────────────────────────────────

    async def run(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> LoopResult:
        """Run the autonomous execution loop for *goal*.

        Parameters
        ----------
        goal:
            Natural-language description of what to accomplish.
        context:
            Optional context passed to the planner and executor.
        task_id:
            Optional explicit task identifier.  A UUID is generated when
            omitted.

        Returns
        -------
        LoopResult
            The outcome of the loop — success, final state, and diagnostics.
        """
        import uuid as _uuid

        self._reset()
        self._task_id = task_id or str(_uuid.uuid4())
        self._goal = goal
        self._context = dict(context or {})
        self._cancel_event = self._event_bus.register_cancel_event(self._task_id)

        logger.info(
            "AutonomousLoop.run: task=%s goal=%s",
            self._task_id[:8],
            goal[:80],
        )

        # ── Phase: PLAN ─────────────────────────────────────────────────
        await self._transition_to(TaskState.QUEUED)
        await self._transition_to(TaskState.PLANNING)
        plan = self._create_plan(goal)
        self._plan = plan
        self._current_step_index = 0
        await self._transition_to(TaskState.READY)
        await self._publish_stage("planning", description=f"Plan created with {len(plan.steps)} steps")

        # ── Main execution loop ─────────────────────────────────────────
        while not self._in_terminal_state():
            if await self._check_cancelled():
                return self._finish(TaskState.CANCELLED)

            # 1) Budget checks
            budget_check = self._check_budgets()
            if budget_check is not None:
                return budget_check

            # 2) Find next step
            step = self._next_pending_step()
            if step is None:
                # All steps completed → VERIFY
                verdict = await self._verify_all()
                if verdict.decision == "PASS":
                    await self._transition_to(TaskState.DONE)
                    logger.info("AutonomousLoop.run: task=%s → DONE", self._task_id[:8])
                    return self._finish(TaskState.DONE, verdict=verdict)
                if verdict.decision == "BLOCKED":
                    await self._transition_to(TaskState.BLOCKED)
                    return self._finish(
                        TaskState.BLOCKED,
                        error=verdict.reason,
                        verdict=verdict,
                    )
                if verdict.decision == "WAITING_USER":
                    await self._transition_to(TaskState.WAITING_USER)
                    return self._finish(
                        TaskState.WAITING_USER,
                        error=verdict.reason,
                        verdict=verdict,
                    )
                # REPLAN
                replan_result = await self._do_replan(verdict)
                if replan_result is not None:
                    return replan_result
                continue

            # 3) EXECUTE step
            step_verdict = await self._execute_step(step)
            if step_verdict is None:
                continue  # step succeeded, advance

            # 4) Handle non-PASS verdict
            loop_exit = await self._handle_verdict(step_verdict, step)
            if loop_exit is not None:
                return loop_exit

        # Terminal state reached via cancellation or budget exhaustion
        if self._state == TaskState.CANCELLED:
            return self._finish(TaskState.CANCELLED)
        if self._state == TaskState.FAILED:
            return self._finish(
                TaskState.FAILED,
                error="Budget or resource limit reached",
            )
        return self._finish(self._state)

    # ── Internal: step execution pipeline ───────────────────────────────

    async def _execute_step(self, step: PlanStep) -> VerificationVerdict | None:
        """Execute a single *step* and return its verdict.

        Returns *None* when the step succeeded (PASS) and the loop should
        advance to the next step.  Returns a :class:`VerificationVerdict`
        otherwise.
        """
        step_id = step.step_id
        attempt = self._attempts.get(step_id, 0)
        logger.info(
            "Executing step %s '%s' (attempt %d/%d)",
            step_id[:8],
            step.title,
            attempt + 1,
            self._max_attempts,
        )

        # ── EXECUTING ───────────────────────────────────────────────
        await self._transition_to(TaskState.TOOL_EXECUTING)
        step.status = "RUNNING"

        await self._publish_tool(step.tool_name, status=f"Running: {step.title}")

        result = await self._executor(step)
        self._tool_calls += 1

        step.status = "SUCCEEDED" if result.technical_success else "FAILED"
        await self._publish_tool(
            step.tool_name,
            status="OK" if result.technical_success else "FAILED",
            exit_code=result.exit_code,
            stdout_preview=(result.stdout or "")[:200],
        )

        # ── OBSERVING ───────────────────────────────────────────────
        await self._transition_to(TaskState.OBSERVING)

        prev_observations = self._step_observations.get(step_id, [])
        observation = self._observer.observe(
            task_id=self._task_id,
            step=step,
            result=result,
            previous_attempts=prev_observations,
        )

        self._step_observations.setdefault(step_id, []).append(observation)
        self._step_results.setdefault(step_id, []).append(result)

        # ── VERIFYING ───────────────────────────────────────────────
        await self._transition_to(TaskState.VERIFYING)

        verdict = self._verifier.verify_step(
            task=self._goal,
            plan=self._plan,  # type: ignore[arg-type]
            step=step,
            result=result,
            observation=observation,
        )

        logger.debug(
            "Step %s verdict: %s (confidence=%.2f)",
            step_id[:8],
            verdict.decision,
            verdict.confidence,
        )

        # PASS → advance to next step
        if verdict.decision == "PASS":
            self._current_step_index += 1
            return None

        return verdict

    async def _handle_verdict(
        self,
        verdict: VerificationVerdict,
        step: PlanStep,
    ) -> LoopResult | None:
        """Route a non-PASS verdict.

        Returns a :class:`LoopResult` when the loop should exit, or
        *None* to continue.
        """
        decision = verdict.decision
        step_id = step.step_id

        if decision == "RETRY":
            return await self._handle_retry(step, verdict)

        if decision == "REPLAN":
            return await self._do_replan(verdict)

        if decision == "WAITING_USER":
            await self._transition_to(TaskState.WAITING_USER)
            return self._finish(
                TaskState.WAITING_USER,
                error=verdict.reason,
                verdict=verdict,
            )

        if decision == "BLOCKED":
            await self._transition_to(TaskState.BLOCKED)
            return self._finish(
                TaskState.BLOCKED,
                error=verdict.reason,
                verdict=verdict,
            )

        if decision == "BUDGET_EXHAUSTED":
            await self._transition_to(TaskState.FAILED)
            return self._finish(
                TaskState.FAILED,
                error=verdict.reason,
                verdict=verdict,
            )

        # Unknown decision → FAILED
        logger.warning(
            "Unknown verdict decision '%s' for step %s; failing task",
            decision,
            step_id[:8],
        )
        await self._transition_to(TaskState.FAILED)
        return self._finish(
            TaskState.FAILED,
            error=f"Unknown verdict: {decision}",
            verdict=verdict,
        )

    async def _handle_retry(
        self,
        step: PlanStep,
        verdict: VerificationVerdict,
    ) -> LoopResult | None:
        """Handle a RETRY verdict.

        Checks the error fingerprint: if the *same* fingerprint has been
        seen before for this step, escalation to REPLAN is forced instead
        of retrying a dead-end.
        """
        step_id = step.step_id
        attempt = self._attempts.get(step_id, 0)

        # Check fingerprint repetition
        results = self._step_results.get(step_id, [])
        if results:
            last_result = results[-1]
            fp = _compute_error_fingerprint(last_result)
            if fp:
                fp_counts = self._fingerprints.setdefault(step_id, {})
                fp_counts[fp] = fp_counts.get(fp, 0) + 1
                if fp_counts[fp] >= 2:
                    # Same fingerprint seen twice → force REPLAN
                    logger.info(
                        "Step %s: repeated error fingerprint %s.. -> REPLAN (not RETRY)",
                        step_id[:8],
                        fp[:12],
                    )
                    step.status = "FAILED"
                    return await self._do_replan(verdict)

        # Check attempt budget
        if attempt >= self._max_attempts - 1:
            logger.info(
                "Step %s: max attempts (%d) exhausted → REPLAN",
                step_id[:8],
                self._max_attempts,
            )
            step.status = "FAILED"
            return await self._do_replan(verdict)

        # Bump attempt counter and schedule retry
        self._attempts[step_id] = attempt + 1
        step.attempt_count = attempt + 1
        step.status = "PENDING"

        await self._transition_to(TaskState.RETRY_SCHEDULED)
        logger.info(
            "Step %s: RETRY attempt %d/%d",
            step_id[:8],
            attempt + 1,
            self._max_attempts,
        )
        return None  # loop continues, step will be re-executed

    async def _do_replan(
        self,
        verdict: VerificationVerdict,
    ) -> LoopResult | None:
        """Execute a replan cycle.

        Returns a :class:`LoopResult` when the replan budget is exhausted
        (task → FAILED), or *None* to continue the loop with the new plan.
        """
        self._replans += 1
        if self._replans > self._max_replans:
            await self._transition_to(TaskState.FAILED)
            logger.warning(
                "Task %s: max replans (%d) exhausted",
                self._task_id[:8],
                self._max_replans,
            )
            return self._finish(
                TaskState.FAILED,
                error=f"Max replans ({self._max_replans}) exceeded",
                verdict=verdict,
            )

        # Collect all failed results as evidence for the planner
        all_results: list[ToolExecutionResult] = []
        for results in self._step_results.values():
            all_results.extend(results)

        instructions = verdict.replan_instructions or verdict.reason or "Replan required"

        await self._transition_to(TaskState.REPLAN_REQUESTED)
        await self._publish_stage("replanning", description=f"Replan #{self._replans}: {instructions[:120]}")

        try:
            new_plan = self._planner.replan(
                task_id=self._task_id,
                instructions=instructions,
                previous_attempts=all_results,
            )
        except Exception as exc:
            logger.exception("Planner.replan() failed for task %s", self._task_id[:8])
            await self._transition_to(TaskState.FAILED)
            return self._finish(
                TaskState.FAILED,
                error=f"Planner.replan() failed: {exc}",
            )

        self._plan = new_plan
        self._current_step_index = 0

        await self._transition_to(TaskState.PLANNING)
        await self._transition_to(TaskState.READY)
        await self._publish_stage(
            "planning",
            description=f"Replan #{self._replans}: {len(new_plan.steps)} new steps",
        )
        logger.info(
            "Task %s: replan #%d → %d steps",
            self._task_id[:8],
            self._replans,
            len(new_plan.steps),
        )
        return None  # loop continues with new plan

    async def _verify_all(self) -> VerificationVerdict:
        """Verify all steps after execution.

        When a plan has acceptance criteria defined at the plan level,
        checks them all.  Otherwise returns PASS for all completed
        steps.
        """
        plan = self._plan
        if not plan:
            return VerificationVerdict(decision="PASS", reason="No plan to verify")

        if not plan.acceptance_criteria:
            # No acceptance criteria — consider all steps succeeded
            return VerificationVerdict(
                decision="PASS",
                passed_criteria=[],
                evidence=[f"All {len(plan.steps)} step(s) completed"],
                reason="All steps executed; no acceptance criteria defined",
                confidence=1.0,
            )

        # Build a synthetic observation from the last result of each step
        passed_ids: list[str] = []
        failed_ids: list[str] = []
        evidence: list[str] = []

        for criterion in plan.acceptance_criteria:
            # Try each step's last result
            criterion_passed = False
            for step in plan.steps:
                results = self._step_results.get(step.step_id, [])
                if not results:
                    continue
                last_result = results[-1]
                observations = self._step_observations.get(step.step_id, [])
                last_obs = observations[-1] if observations else None
                if last_obs is None:
                    continue

                check = self._verifier._check_single_criterion(
                    criterion,
                    plan,
                    step,
                    last_result,
                    last_obs,
                )
                if check is True:
                    criterion_passed = True
                    break

            if criterion_passed:
                passed_ids.append(criterion.id)
                evidence.append(f"Criterion '{criterion.id}' passed")
            else:
                failed_ids.append(criterion.id)
                evidence.append(f"Criterion '{criterion.id}' failed")

        all_mandatory_passed = self._verifier._all_mandatory_passed(
            plan.acceptance_criteria,
            passed_ids,
        )

        if all_mandatory_passed:
            return VerificationVerdict(
                decision="PASS",
                passed_criteria=passed_ids,
                failed_criteria=failed_ids,
                evidence=evidence,
                reason=f"All mandatory criteria satisfied ({len(passed_ids)} passed)",
                confidence=1.0,
            )

        # Some mandatory criteria failed → REPLAN
        return VerificationVerdict(
            decision="REPLAN",
            passed_criteria=passed_ids,
            failed_criteria=failed_ids,
            evidence=evidence,
            reason=f"Mandatory criteria failed: {', '.join(failed_ids)}",
            confidence=0.5,
        )

    # ── Plan management ─────────────────────────────────────────────────

    def _create_plan(self, goal: str) -> ExecutionPlan:
        """Create an execution plan for *goal* via the planner."""
        return self._planner.create_plan(
            goal=goal,
            context=self._context,
        )

    def _next_pending_step(self) -> PlanStep | None:
        """Return the next pending step, or *None* if all steps are done."""
        plan = self._plan
        if plan is None:
            return None
        for i in range(self._current_step_index, len(plan.steps)):
            step = plan.steps[i]
            if step.status in ("PENDING", "RUNNING"):
                return step
        return None

    # ── Budget checks ──────────────────────────────────────────────────

    def _check_budgets(self) -> LoopResult | None:
        """Check all budget limits.  Returns a :class:`LoopResult` when a
        limit is exceeded, or *None* to continue."""
        if self._tool_calls >= self._max_tool_calls:
            logger.warning(
                "Task %s: tool-call budget (%d) exhausted",
                self._task_id[:8],
                self._max_tool_calls,
            )
            return self._finish(
                TaskState.FAILED,
                error=f"Tool-call budget ({self._max_tool_calls}) exhausted",
            )
        return None

    def _in_terminal_state(self) -> bool:
        """Return *True* when the current state is terminal/absorbing."""
        from antigona.durable.state_machine import TERMINAL_STATES

        return self._state in TERMINAL_STATES

    # ── State machine helpers ──────────────────────────────────────────

    async def _transition_to(self, target: TaskState) -> None:
        """Transition the state machine to *target*.

        Logs the transition and publishes a ``StageChanged`` event.
        Silent on illegal transitions (logs a warning).
        """
        from_state = self._state
        try:
            check_task_transition(from_state, target, cancellation_requested=False)
        except StateMachineInvalidTransition:
            logger.warning(
                "State transition %s -> %s is forbidden; current=%s",
                from_state.value,
                target.value,
                self._state.value,
            )
            return
        self._state = target
        logger.debug(
            "State: %s -> %s [task=%s]",
            from_state.value,
            target.value,
            self._task_id[:8],
        )
        await self._event_bus.publish(
            StageChanged(
                correlation_id=self._task_id,
                operation_id=self._task_id,
                stage=target.value.lower(),
                step=self._current_step_index,
                total=len(self._plan.steps) if self._plan else 0,
                description=f"{from_state.value} → {target.value}",
            ),
        )

    async def _check_cancelled(self) -> bool:
        """Check if cancellation was requested for this task.

        Returns *True* when the task should terminate.
        """
        if self._event_bus.is_cancelled(self._task_id):
            logger.info("Task %s: cancelled via EventBus", self._task_id[:8])
            await self._transition_to(TaskState.CANCELLED)
            return True
        if self._cancel_event and self._cancel_event.is_set():
            logger.info("Task %s: cancelled via cancel event", self._task_id[:8])
            await self._transition_to(TaskState.CANCELLED)
            return True
        return False

    # ── Events ─────────────────────────────────────────────────────────

    async def _publish_stage(
        self,
        stage: str,
        *,
        description: str = "",
    ) -> None:
        """Publish a ``StageChanged`` event."""
        await self._event_bus.publish(
            StageChanged(
                correlation_id=self._task_id,
                operation_id=self._task_id,
                stage=stage,
                step=self._current_step_index,
                total=len(self._plan.steps) if self._plan else 0,
                description=description,
            ),
        )

    async def _publish_tool(
        self,
        tool_name: str,
        *,
        status: str = "",
        exit_code: int | None = None,
        stdout_preview: str | None = None,
    ) -> None:
        """Publish a ``ToolProgress`` event."""
        await self._event_bus.publish(
            ToolProgress(
                correlation_id=self._task_id,
                operation_id=self._task_id,
                tool_name=tool_name,
                status=status,
                exit_code=exit_code,
                stdout_preview=stdout_preview,
            ),
        )

    # ── Finalisation ───────────────────────────────────────────────────

    def _finish(
        self,
        state: TaskState,
        *,
        error: str = "",
        verdict: VerificationVerdict | None = None,
    ) -> LoopResult:
        """Build and return a :class:`LoopResult`, publishing a completion event."""
        self._final_verdict = verdict
        is_success = state == TaskState.DONE

        result = LoopResult(
            success=is_success,
            final_state=state,
            plan=self._plan,
            iterations=self._current_step_index,
            tool_calls=self._tool_calls,
            replans=self._replans,
            error=error,
            verdict=verdict,
        )

        # Fire-and-forget completion event
        asyncio.ensure_future(
            self._event_bus.publish(
                TaskCompleted(
                    correlation_id=self._task_id,
                    task_id=self._task_id,
                    status=state.value,
                    result={
                        "success": is_success,
                        "state": state.value,
                        "tool_calls": self._tool_calls,
                        "replans": self._replans,
                        "error": error,
                    },
                ),
            ),
        )

        logger.info(
            "AutonomousLoop.finish: task=%s state=%s success=%s "
            "tools=%d replans=%d error=%s",
            self._task_id[:8],
            state.value,
            is_success,
            self._tool_calls,
            self._replans,
            error or "(none)",
        )
        return result

    # ── Reset ──────────────────────────────────────────────────────────

    def _reset(self) -> None:
        """Reset all per-task mutable state."""
        self._state = TaskState.CREATED
        self._plan = None
        self._current_step_index = 0
        self._tool_calls = 0
        self._replans = 0
        self._attempts.clear()
        self._fingerprints.clear()
        self._step_observations.clear()
        self._step_results.clear()
        self._cancel_event = None
        self._final_verdict = None


# ── Default executor ─────────────────────────────────────────────────────────


async def _default_executor(step: PlanStep) -> ToolExecutionResult:
    """Default plan-step executor using asyncio subprocess.

    Handles two tool types:
    * ``sandbox.shell`` — runs the ``command`` argument as a subprocess.
    * ``workspace.read_file`` — reads a file at ``arguments["path"]``.
    """
    import time as _time

    started_at = _time.time()
    tool_name = step.tool_name
    arguments = step.arguments

    if tool_name == "sandbox.shell":
        command = arguments.get("command", "")
        if not command:
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                exit_code=1,
                stderr="No command specified",
                technical_success=False,
                error_type="invalid_arguments",
                error_message="sandbox.shell: 'command' argument is empty",
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=60.0,
            )
            finished_at = _time.time()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                command=command,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                technical_success=exit_code == 0,
            )
        except TimeoutError:
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                command=command,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=None,
                stderr="Command timed out after 60s",
                technical_success=False,
                timed_out=True,
                error_type="timeout",
                error_message="Command timed out after 60s",
            )
        except Exception as exc:
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                command=command,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=None,
                stderr=str(exc),
                technical_success=False,
                error_type="execution_error",
                error_message=str(exc),
            )

    if tool_name == "workspace.read_file":
        path = arguments.get("path", "")
        if not path:
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                exit_code=1,
                stderr="No path specified",
                technical_success=False,
                error_type="invalid_arguments",
                error_message="workspace.read_file: 'path' argument is empty",
            )
        import os as _os

        if not _os.path.isfile(path):
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=1,
                stderr=f"File not found: {path}",
                technical_success=False,
                error_type="file_not_found",
                error_message=f"File not found: {path}",
            )
        try:
            import aiofiles as _aiofiles  # type: ignore[import-untyped]
        except ImportError:
            # Fallback: synchronous read wrapped in executor
            import functools as _functools

            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(
                None,
                _functools.partial(_read_file_sync, path),
            )
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=0,
                stdout=content,
                technical_success=True,
            )

        try:
            async with _aiofiles.open(path, encoding="utf-8") as f:
                content = await f.read()
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=0,
                stdout=content,
                technical_success=True,
            )
        except Exception as exc:
            finished_at = _time.time()
            return ToolExecutionResult(
                task_id="",
                step_id=step.step_id,
                tool_name=tool_name,
                arguments=arguments,
                started_at=__import__("datetime").datetime.fromtimestamp(started_at),
                finished_at=__import__("datetime").datetime.fromtimestamp(finished_at),
                duration_ms=int((finished_at - started_at) * 1000),
                exit_code=1,
                stderr=str(exc),
                technical_success=False,
                error_type="read_error",
                error_message=str(exc),
            )

    # Unknown tool type
    return ToolExecutionResult(
        task_id="",
        step_id=step.step_id,
        tool_name=tool_name,
        arguments=arguments,
        exit_code=1,
        stderr=f"Unknown tool: {tool_name}",
        technical_success=False,
        error_type="tool_not_found",
        error_message=f"No executor registered for tool '{tool_name}'",
    )


def _read_file_sync(path: str) -> str:
    """Synchronous file read for executor fallback."""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Module-level re-exports ─────────────────────────────────────────────

__all__ = [
    "AutonomousLoop",
    "LoopResult",
    "MAX_ATTEMPTS_PER_STEP",
    "MAX_REPLANS_PER_TASK",
    "MAX_TOTAL_TOOL_CALLS",
]
