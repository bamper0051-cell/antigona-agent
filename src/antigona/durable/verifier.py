"""Pure-logic Verifier for the autonomous execution loop.

The Verifier is the **only** component that may gate the ``VERIFYING -> DONE``
transition (see AGENTS.md).  It inspects a completed step's tool result and
observation against the plan's acceptance criteria and renders a
:class:`VerificationVerdict` that drives the next state-machine transition.
"""

from __future__ import annotations

from antigona.durable.execution_models import (
    AcceptanceCriterion,
    ExecutionPlan,
    Observation,
    PlanStep,
    ToolExecutionResult,
    VerificationVerdict,
)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Error categories whose presence in ``ToolExecutionResult.error_type``
#: signals a **transient** failure where a retry of the same step may succeed.
_RETRYABLE_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "timeout",
        "network_error",
        "rate_limited",
        "service_unavailable",
        "file_locked",
        "concurrent_modification",
        "database_connection",
    }
)

#: Error categories whose presence signals a **logical** failure that requires
#: a replan rather than a retry.
_REPLAN_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "wrong_tool",
        "invalid_arguments",
        "syntax_error",
        "wrong_path",
        "permission_denied",
        "file_not_found",
        "resource_not_found",
        "bad_request",
        "schema_validation",
    }
)


# ── Verifier ───────────────────────────────────────────────────────────────────


class Verifier:
    """Pure-logic verifier for autonomous execution steps.

    The Verifier is stateless by design — all context is passed explicitly
    on every call, making it trivially testable and safe to reuse across
    multiple plan revisions.

    Usage::

        verdict = Verifier.verify_step(
            task="Create a report from /tmp/data.csv",
            plan=plan,
            step=current_step,
            result=tool_result,
            observation=obs,
        )

    Only a ``PASS`` verdict may lead to ``VERIFYING -> DONE``; all other
    decisions route the execution loop to retry, replan, wait for user input,
    or report a block.
    """

    # ── Main entry point ─────────────────────────────────────────────────

    @staticmethod
    def verify_step(
        task: str,
        plan: ExecutionPlan,
        step: PlanStep,
        result: ToolExecutionResult,
        observation: Observation,
    ) -> VerificationVerdict:
        """Verify a completed step and return a verdict.

        Parameters
        ----------
        task:
            The original user-supplied task / goal text.
        plan:
            The :class:`ExecutionPlan` containing the step and its acceptance
            criteria.
        step:
            The :class:`PlanStep` that was just executed.
        result:
            The raw :class:`ToolExecutionResult` produced by the step.
        observation:
            The :class:`Observation` distilled from *result* by the
            observation layer.

        Returns
        -------
        VerificationVerdict
            The decision that drives the next state-machine transition.
            Only ``PASS`` may lead to ``DONE``.
        """
        _ = task  # available for future task-aware heuristics

        # ── Phase 1: Technical-failure classification ────────────────────
        if not result.technical_success:
            return Verifier._classify_technical_failure(
                plan,
                step,
                result,
                observation,
            )

        # ── Phase 2: Observation-based failures ──────────────────────────
        if observation.errors and observation.retryable:
            return Verifier._retry_verdict(
                step,
                observation,
                reason=(f"Retryable error in step '{step.title}': {'; '.join(observation.errors)}"),
            )

        if observation.errors and not observation.retryable:
            return Verifier._replan_verdict(
                plan,
                reason=(
                    f"Non-retryable error in step '{step.title}': {'; '.join(observation.errors)}"
                ),
            )

        # ── Phase 3: Acceptance-criteria checks ──────────────────────────
        return Verifier._check_acceptance_criteria(
            plan,
            step,
            result,
            observation,
        )

    # ── Phase 1 helpers ──────────────────────────────────────────────────

    @classmethod
    def _classify_technical_failure(
        cls,
        plan: ExecutionPlan,
        step: PlanStep,
        result: ToolExecutionResult,
        observation: Observation,
    ) -> VerificationVerdict:
        """Classify a *technical_success* == *False* result."""
        error_type = (result.error_type or "").lower()

        # Transient infrastructure error → RETRY
        if error_type in _RETRYABLE_ERROR_TYPES:
            return cls._retry_verdict(
                step,
                observation,
                reason=(
                    f"Transient infrastructure error '{result.error_type}' "
                    f"in step '{step.title}': "
                    f"{result.error_message or '(no detail)'}"
                ),
            )

        # Logical / configuration error → REPLAN
        if error_type in _REPLAN_ERROR_TYPES:
            return cls._replan_verdict(
                plan,
                reason=(
                    f"Logical error '{result.error_type}' in step "
                    f"'{step.title}': "
                    f"{result.error_message or '(no detail)'}"
                ),
            )

        # Timed out but not in the retryable list → treat as RETRY anyway
        if result.timed_out:
            return cls._retry_verdict(
                step,
                observation,
                reason=f"Step '{step.title}' timed out",
            )

        # Cancelled — inform the planner via replan
        if result.cancelled:
            return cls._replan_verdict(
                plan,
                reason=f"Step '{step.title}' was cancelled",
            )

        # Unknown technical failure → RETRY as safest default
        return cls._retry_verdict(
            step,
            observation,
            reason=(
                f"Unknown technical failure in step '{step.title}': "
                f"{result.error_message or '(no detail)'}"
            ),
        )

    # ── Phase 3 helpers ──────────────────────────────────────────────────

    @classmethod
    def _check_acceptance_criteria(
        cls,
        plan: ExecutionPlan,
        step: PlanStep,
        result: ToolExecutionResult,
        observation: Observation,
    ) -> VerificationVerdict:
        """Evaluate acceptance criteria and render a verdict."""
        if not plan.acceptance_criteria:
            # No explicit criteria — use the observation's signal
            if observation.expected_result_found:
                return cls._pass_verdict(
                    evidence=[f"Step '{step.title}' completed successfully"],
                    reason=("All criteria satisfied (no explicit acceptance criteria defined)"),
                )
            return cls._replan_verdict(
                plan,
                reason=(f"Step '{step.title}' completed but expected result was not found"),
            )

        passed_ids: list[str] = []
        failed_ids: list[str] = []
        evidence: list[str] = []

        for criterion in plan.acceptance_criteria:
            check_result = cls._check_single_criterion(
                criterion,
                plan,
                step,
                result,
                observation,
            )
            if check_result is True:
                passed_ids.append(criterion.id)
                evidence.append(f"Criterion '{criterion.id}' passed: {criterion.description}")
            else:
                failed_ids.append(criterion.id)
                evidence.append(
                    f"Criterion '{criterion.id}' failed: "
                    f"{criterion.description} — "
                    f"{check_result or 'not satisfied'}"
                )

        all_mandatory_passed = cls._all_mandatory_passed(
            plan.acceptance_criteria,
            passed_ids,
        )

        if all_mandatory_passed:
            return cls._pass_verdict(
                evidence=evidence,
                passed_criteria=passed_ids,
                failed_criteria=failed_ids,
                reason=(
                    f"All mandatory acceptance criteria satisfied "
                    f"({len(passed_ids)} passed, "
                    f"{len(failed_ids)} advisory failed)"
                ),
            )

        # Some mandatory criteria failed — classify the failure
        return cls._classify_criterion_failure(
            plan,
            step,
            observation,
            passed_ids,
            failed_ids,
            evidence,
        )

    @staticmethod
    def _check_single_criterion(
        criterion: AcceptanceCriterion,
        plan: ExecutionPlan,
        step: PlanStep,
        result: ToolExecutionResult,
        observation: Observation,
    ) -> bool | str:
        """Check a single acceptance criterion.

        Returns *True* if the criterion passes, or a string explanation of
        why it failed.
        """
        _ = plan  # available for future criterion-aware checks

        description_lower = criterion.description.lower()

        # File-existence check
        if "file exists" in description_lower or "file created" in description_lower:
            return observation.target_exists is True

        # Output-not-empty check
        if "output not empty" in description_lower or "result not empty" in description_lower:
            return not observation.output_empty

        # Expected-result match
        if "expected result" in description_lower or "correct output" in description_lower:
            return observation.expected_result_found

        # Artifact validation
        if "artifact" in description_lower and "valid" in description_lower:
            return observation.artifacts_valid

        # Tool-specific verification method fallback
        if step.verification_method == "file_exists":
            return observation.target_exists is True
        if step.verification_method == "output_not_empty":
            return not observation.output_empty
        if step.verification_method == "exit_code_zero":
            if result.exit_code is not None:
                return result.exit_code == 0
            return False

        # Generic fallback: use step's expected_result + observation
        if observation.expected_result_found:
            return True

        return f"criterion '{criterion.id}' could not be verified — no matching signal"

    @staticmethod
    def _all_mandatory_passed(
        criteria: list[AcceptanceCriterion],
        passed_ids: list[str],
    ) -> bool:
        """Return *True* when every mandatory criterion is in *passed_ids*."""
        passed_set = set(passed_ids)
        for c in criteria:
            if c.required and c.id not in passed_set:
                return False
        return True

    @classmethod
    def _classify_criterion_failure(
        cls,
        plan: ExecutionPlan,
        step: PlanStep,
        observation: Observation,
        passed_ids: list[str],
        failed_ids: list[str],
        evidence: list[str],
    ) -> VerificationVerdict:
        """Classify why mandatory criteria failed and choose the right verdict."""
        # Transient condition → RETRY
        if observation.retryable:
            return cls._retry_verdict(
                step,
                observation,
                evidence=evidence,
                passed_criteria=passed_ids,
                failed_criteria=failed_ids,
                reason=(
                    f"Mandatory criteria failed with retryable condition in step '{step.title}'"
                ),
            )

        # User intervention needed — low confidence, empty output, or
        # the observation layer explicitly asked for it
        if observation.suggested_action == "wait_user" or (
            observation.confidence < 0.3 and observation.output_empty
        ):
            return cls._waiting_user_verdict(
                evidence=evidence,
                passed_criteria=passed_ids,
                failed_criteria=failed_ids,
                reason=(
                    f"Cannot determine failure cause for step '{step.title}' — user input required"
                ),
                user_question=(
                    f"Step '{step.title}' failed with low-confidence "
                    f"result. How should the planner proceed?"
                ),
            )

        # Replan — the plan's approach was flawed
        return cls._replan_verdict(
            plan,
            evidence=evidence,
            passed_criteria=passed_ids,
            failed_criteria=failed_ids,
            reason=(
                f"Mandatory acceptance criteria failed for step "
                f"'{step.title}': {'; '.join(failed_ids)}"
            ),
        )

    # ── Verdict constructors ─────────────────────────────────────────────

    @staticmethod
    def _pass_verdict(
        *,
        evidence: list[str] | None = None,
        passed_criteria: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        reason: str = "",
    ) -> VerificationVerdict:
        """Build a ``PASS`` verdict."""
        return VerificationVerdict(
            decision="PASS",
            passed_criteria=passed_criteria or [],
            failed_criteria=failed_criteria or [],
            evidence=evidence or [],
            reason=reason,
            confidence=1.0,
        )

    @staticmethod
    def _retry_verdict(
        step: PlanStep,
        observation: Observation,
        *,
        evidence: list[str] | None = None,
        passed_criteria: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        reason: str = "",
    ) -> VerificationVerdict:
        """Build a ``RETRY`` verdict with a retry strategy."""
        return VerificationVerdict(
            decision="RETRY",
            passed_criteria=passed_criteria or [],
            failed_criteria=failed_criteria or [],
            evidence=evidence or [],
            reason=reason or f"Retryable failure in step '{step.title}'",
            retry_strategy=_select_retry_strategy(step, observation),
            confidence=0.4,
        )

    @staticmethod
    def _replan_verdict(
        plan: ExecutionPlan,
        *,
        evidence: list[str] | None = None,
        passed_criteria: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        reason: str = "",
    ) -> VerificationVerdict:
        """Build a ``REPLAN`` verdict with structured replan instructions."""
        return VerificationVerdict(
            decision="REPLAN",
            passed_criteria=passed_criteria or [],
            failed_criteria=failed_criteria or [],
            evidence=evidence or [],
            reason=reason or "Plan revision required",
            replan_instructions=_build_replan_instructions(
                plan,
                reason or "",
            ),
            confidence=0.6,
        )

    @staticmethod
    def _waiting_user_verdict(
        *,
        evidence: list[str] | None = None,
        passed_criteria: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        reason: str = "",
        user_question: str = "",
    ) -> VerificationVerdict:
        """Build a ``WAITING_USER`` verdict with a question for the user."""
        return VerificationVerdict(
            decision="WAITING_USER",
            passed_criteria=passed_criteria or [],
            failed_criteria=failed_criteria or [],
            evidence=evidence or [],
            reason=reason,
            user_question=user_question,
            confidence=0.3,
        )

    @staticmethod
    def _blocked_verdict(
        plan: ExecutionPlan,
        *,
        reason: str = "",
    ) -> VerificationVerdict:
        """Build a ``BLOCKED`` verdict with an explanation."""
        _ = plan  # available for future context-aware blocked messages
        return VerificationVerdict(
            decision="BLOCKED",
            reason=reason or "Task cannot proceed",
            confidence=1.0,
        )

    # ── Public utility ───────────────────────────────────────────────────

    @staticmethod
    def all_criteria_passed(
        criteria: list[AcceptanceCriterion],
        results: dict[str, bool],
    ) -> bool:
        """Return *True* when every mandatory criterion is satisfied.

        Parameters
        ----------
        criteria:
            The list of acceptance criteria to evaluate.
        results:
            A mapping of criterion ID → bool indicating whether that
            criterion was satisfied.

        Returns
        -------
        bool
            *True* iff all **required** criteria have a *True* result.
            Advisory criteria (``required=False``) are ignored.
        """
        for c in criteria:
            if not c.required:
                continue
            verdict = results.get(c.id)
            if verdict is not True:
                return False
        return True


# ── Module-level helpers (kept outside the class for testability) ──────────────


def _select_retry_strategy(step: PlanStep, observation: Observation) -> str:
    """Determine the best retry strategy for the given failure context."""
    if observation.suggested_action == "retry" and observation.summary:
        return observation.summary
    if step.attempt_count < 2:
        return "exponential_backoff"
    return "skip_confirm"


def _build_replan_instructions(plan: ExecutionPlan, failure_reason: str) -> str:
    """Build structured replan instructions from the failure context."""
    completed = [s for s in plan.steps if s.status == "SUCCEEDED"]
    failed = [s for s in plan.steps if s.status == "FAILED"]
    pending = [s for s in plan.steps if s.status == "PENDING"]

    parts: list[str] = [f"Replan required: {failure_reason}"]
    if completed:
        parts.append(f"Completed steps ({len(completed)}): {', '.join(s.title for s in completed)}")
    if failed:
        parts.append(f"Failed steps ({len(failed)}): {', '.join(s.title for s in failed)}")
    if pending:
        parts.append(f"Pending steps ({len(pending)}): {', '.join(s.title for s in pending)}")
    parts.append(f"Original goal: {plan.goal}")
    return "\n".join(parts)


# ── Module-level re-exports ────────────────────────────────────────────────────

__all__ = [
    "Verifier",
]
