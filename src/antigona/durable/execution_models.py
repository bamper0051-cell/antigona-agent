"""Data models for the autonomous execution cycle.

This module defines the core dataclasses that drive the agent's autonomous
execution loop — from planning through tool execution, observation, and
verification.  Every model is a plain :class:`~dataclasses.dataclass` with
full type annotations; these are **not** SQLAlchemy ORM models.

The five principal types form a clear lifecycle:

1. :class:`AcceptanceCriterion` — a single success criterion a plan must satisfy.
2. :class:`PlanStep` — one atomic tool invocation within a plan.
3. :class:`ExecutionPlan` — a complete plan comprising steps and criteria.
4. :class:`ToolExecutionResult` — the outcome of running one tool call.
5. :class:`Observation` — a structured interpretation of a result.
6. :class:`VerificationVerdict` — the loop's decision after verification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ── Acceptance criterion ────────────────────────────────────────────────────────


@dataclass
class AcceptanceCriterion:
    """A single success criterion that an execution plan must satisfy.

    Every :class:`ExecutionPlan` carries a list of these criteria.  After
    all steps have completed, the planner or verifier checks each criterion
    against the observed state; any failure may trigger a retry or replan.

    Attributes:
        id: Unique identifier for this criterion.
        description: Human-readable statement of what must be true.
        required: Whether this criterion is mandatory (*True*) or advisory
            (*False*).  Defaults to *True*.
    """

    id: str
    description: str
    required: bool = True


# ── Plan step ───────────────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    """One atomic tool-invocation step within an :class:`ExecutionPlan`.

    Each step specifies exactly which tool to call, with what arguments,
    and how to verify the result.  The execution loop runs steps in order
    and may retry a failing step up to *max_attempts* times.

    Attributes:
        step_id: UUID identifying this step (auto-generated if not provided).
        title: Short human-readable label describing the step's purpose.
        tool_name: Fully-qualified tool identifier (e.g. ``workspace.write_text``).
        arguments: Keyword arguments passed to the tool at invocation.
        expected_result: Description of the expected outcome, used during
            verification.  Structured as a dict for programmatic matching.
        verification_method: Name of the verification strategy to apply
            (e.g. ``"file_exists"``, ``"output_contains"``, ``"exit_code_zero"``).
        status: Current lifecycle status.  One of ``PENDING``, ``RUNNING``,
            ``SUCCEEDED``, ``FAILED``.
        attempt_count: How many times this step has already been attempted
            (zero-based).  Defaults to 0.
        max_attempts: Maximum number of attempts before the step is
            considered failed.  Defaults to 3.
    """

    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    expected_result: dict[str, Any] = field(default_factory=dict)
    verification_method: str = "default"
    status: str = "PENDING"
    attempt_count: int = 0
    max_attempts: int = 3


# ── Execution plan ──────────────────────────────────────────────────────────────


@dataclass
class ExecutionPlan:
    """A complete execution plan for an autonomous task.

    The plan binds a high-level goal to an ordered list of tool-invocation
    steps and a set of acceptance criteria.  Multiple revisions of the same
    plan may exist (e.g. after a replan), distinguished by the *revision*
    counter.

    Attributes:
        plan_id: UUID for this plan.
        task_id: UUID of the parent task this plan serves.
        revision: Monotonically increasing revision number.  Bumped on
            replan.  Defaults to 1.
        goal: High-level natural-language goal the plan is designed to
            accomplish.
        steps: Ordered list of :class:`PlanStep` instances that make up
            the plan's execution body.
        acceptance_criteria: List of :class:`AcceptanceCriterion` that
            define success for this plan.
        created_at: Timestamp (UTC, timezone-aware) when the plan was
            created.
        reason: Free-text explanation of *why* this plan was created —
            useful for audit trails and replan annotations.
    """

    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = 1
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now())
    reason: str = ""


# ── Tool execution result ───────────────────────────────────────────────────────


@dataclass
class ToolExecutionResult:
    """The complete record of a single tool invocation.

    Captures everything about an execution attempt — inputs, outputs,
    timing, and disposition — so the observation and verification layers
    can make informed decisions about retries, replans, or escalation.

    Attributes:
        execution_id: Unique identifier for this invocation.
        task_id: UUID of the parent task.
        step_id: UUID of the :class:`PlanStep` this result belongs to.
        tool_name: Fully-qualified tool identifier that was called.
        arguments: Actual arguments passed to the tool.
        command: Optional shell command or subprocess invocation string,
            if the tool wraps a shell command.  *None* for non-shell tools.
        started_at: Timestamp (UTC, timezone-aware) when execution began.
        finished_at: Timestamp (UTC, timezone-aware) when execution ended.
        duration_ms: Wall-clock duration in milliseconds.
        exit_code: Process exit code, or *None* if not applicable.
        stdout: Captured standard output text.
        stderr: Captured standard error text.
        artifacts: List of artifact dicts produced by the tool
            (e.g. ``{"path": "/tmp/output.png", "type": "image"}``).
        side_effects: List of side-effect dicts describing observable
            changes to the environment.
        timed_out: *True* if the execution was terminated by a timeout.
        cancelled: *True* if the execution was explicitly cancelled.
        sandboxed: *True* if the execution ran in a sandboxed environment.
        technical_success: *True* if the tool ran without infrastructure
            errors (i.e. the tool itself did not crash or hang).  Does
            **not** imply the *result* was correct.
        error_type: Machine-readable error category (e.g. ``"timeout"``,
            ``"permission_denied"``, ``"tool_not_found"``), or *None*.
        error_message: Human-readable error description, or *None*.
    """

    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    step_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    command: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now())
    finished_at: datetime = field(default_factory=lambda: datetime.now())
    duration_ms: int = 0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    sandboxed: bool = False
    technical_success: bool = False
    error_type: str | None = None
    error_message: str | None = None


# ── Observation ─────────────────────────────────────────────────────────────────


@dataclass
class Observation:
    """A structured interpretation of a :class:`ToolExecutionResult`.

    The observation layer inspects a raw result and distils it into
    actionable signals for the verification loop — what succeeded, what
    failed, whether the failure is retryable, and what action the loop
    should take next.

    Attributes:
        step_id: UUID of the step that produced this observation.
        attempt: Which attempt number (zero-based) generated this observation.
        summary: One- or two-sentence natural-language summary of what
            happened.
        facts: List of concrete, verifiable facts extracted from the
            result (e.g. ``"File /tmp/output.json exists"``).
        errors: List of error messages or descriptions, if any.
        warnings: List of non-fatal warnings or anomalies.
        output_empty: *True* if the tool produced no stdout/result.
        target_exists: *True* if the expected output target (file,
            resource, etc.) was found.  *None* when not applicable.
        expected_result_found: *True* if the expected result pattern
            matched the actual output.
        artifacts_valid: *True* if all produced artifacts passed
            validation (checksum, size, format, etc.).
        retryable: *True* if a retry of the same step may reasonably
            succeed (e.g. transient network failure).
        suggested_action: Recommended next action for the loop —
            ``"retry"``, ``"replan"``, ``"wait_user"``, ``"abort"``, etc.
        confidence: Confidence score in [0.0, 1.0] representing how
            reliable this observation is.
    """

    step_id: str = ""
    attempt: int = 0
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_empty: bool = True
    target_exists: bool | None = None
    expected_result_found: bool = False
    artifacts_valid: bool = False
    retryable: bool = False
    suggested_action: str = ""
    confidence: float = 0.0


# ── Verification verdict ────────────────────────────────────────────────────────


@dataclass
class VerificationVerdict:
    """The final decision produced by the verification layer.

    After all steps have been executed and the results observed, the
    verifier renders a verdict.  The verdict determines the next action
    for the execution loop — pass, retry, replan, wait for the user,
    request approval, or report a block.

    The *decision* field uses a controlled vocabulary:

    * ``PASS`` — all acceptance criteria satisfied; task is complete.
    * ``RETRY`` — transient failure detected; retry the current step.
    * ``REPLAN`` — plan is flawed or conditions changed; generate a new
      plan revision.
    * ``WAITING_USER`` — need user input before proceeding.
    * ``APPROVAL_REQUIRED`` — a high-risk action needs human authorisation.
    * ``BLOCKED`` — unresolvable environment or policy block.
    * ``BUDGET_EXHAUSTED`` — step or task budget (time, retries, tokens)
      has been consumed.

    Attributes:
        decision: Verdict decision (see controlled vocabulary above).
        passed_criteria: IDs of acceptance criteria that passed.
        failed_criteria: IDs of acceptance criteria that failed.
        evidence: List of evidence strings supporting the verdict
            (e.g. log excerpts, diff snippets, check results).
        reason: Comprehensive human-readable justification for the
            verdict, covering both passed and failed criteria.
        retry_strategy: When *decision* is ``RETRY``, describes the
            retry approach (e.g. ``"exponential_backoff"``,
            ``"skip_confirm"``).  *None* otherwise.
        replan_instructions: When *decision* is ``REPLAN``, provides
            guidance for the next plan revision.  *None* otherwise.
        user_question: When *decision* is ``WAITING_USER`` or
            ``APPROVAL_REQUIRED``, the question to present to the user.
            *None* otherwise.
        confidence: Confidence score in [0.0, 1.0] representing the
            verifier's certainty in this verdict.
    """

    decision: str = "PASS"
    passed_criteria: list[str] = field(default_factory=list)
    failed_criteria: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    reason: str = ""
    retry_strategy: str | None = None
    replan_instructions: str | None = None
    user_question: str | None = None
    confidence: float = 0.0


# ── Module-level re-exports ─────────────────────────────────────────────────────

__all__ = [
    "AcceptanceCriterion",
    "ExecutionPlan",
    "Observation",
    "PlanStep",
    "ToolExecutionResult",
    "VerificationVerdict",
]
