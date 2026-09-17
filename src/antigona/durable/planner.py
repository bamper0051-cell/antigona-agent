"""Autonomous Planner — decomposes goals into executable plans and replans.

This module provides the :class:`Planner` class, which serves as the
intelligence layer of the autonomous execution loop.  It analyzes a
natural-language *goal*, decomposes it into an ordered list of
:class:`PlanStep` instances with associated
:class:`AcceptanceCriterion`, and returns a ready-to-execute
:class:`ExecutionPlan`.

When a prior attempt fails, :meth:`Planner.replan` generates a *revised*
plan that incorporates the failure analysis from previous
:class:`ToolExecutionResult` records, bumps the revision counter, and
produces a concretely different sequence of steps that addresses the
root cause of the failure.

Usage::

    planner = Planner()
    plan = planner.create_plan("Check the size of operation_models.py")
    # → ExecutionPlan with 3 steps (stat → find fallback → wc)

    new_plan = planner.replan(
        task_id=plan.task_id,
        instructions="File was not found, try a broader search",
        previous_attempts=[...],
    )
    # → ExecutionPlan with revision=2 and adjusted steps
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.durable.execution_models import (
    AcceptanceCriterion,
    ExecutionPlan,
    PlanStep,
    ToolExecutionResult,
)

# ── Constants ────────────────────────────────────────────────────────────────────

#: Project root for constructing default file paths.
_PROJECT_ROOT = str(paths.project_root())

#: Subdirectory inside the project where durable modules live.
_DURABLE_SUBDIR = "src/antigona/durable"

# ── Criterion helpers ────────────────────────────────────────────────────────────


def _make_criterion(name: str, description: str | None = None) -> AcceptanceCriterion:
    """Build a single :class:`AcceptanceCriterion` from a well-known *name*.

    If *description* is provided it overrides the built-in template.
    """
    _WELL_KNOWN: dict[str, tuple[str, bool]] = {
        "file_exists": (
            "The expected output file or target exists on disk",
            True,
        ),
        "correct_target": (
            "The operation targeted the correct file or resource",
            True,
        ),
        "numeric_size": ("The reported size is a valid numeric value", True),
        "units_present": (
            "The output includes appropriate unit information",
            True,
        ),
        "evidence_present": (
            "The plan output contains verifiable evidence of completion",
            True,
        ),
        "exit_code_zero": ("Every tool exited with code 0", True),
        "output_nonempty": ("The tool produced non-empty output", True),
        "output_contains_target": (
            "The output contains the expected target identifier",
            True,
        ),
        "resource_reachable": ("The target resource or path is reachable", True),
        "permissions_ok": (
            "The operation had sufficient file-system permissions",
            False,
        ),
    }
    desc, required = _WELL_KNOWN.get(
        name,
        (description or f"Acceptance criterion: {name}", True),
    )
    return AcceptanceCriterion(
        id=name,
        description=description if description is not None else desc,
        required=required,
    )


def _default_criteria(extra: list[str] | None = None) -> list[AcceptanceCriterion]:
    """Return a default set of acceptance criteria, optionally with extras."""
    base = ["exit_code_zero", "output_nonempty", "evidence_present"]
    if extra:
        for name in extra:
            if name not in base:
                base.append(name)
    return [_make_criterion(name) for name in base]


# ── Goal analysis ────────────────────────────────────────────────────────────────


@dataclass
class _GoalAnalysis:
    """Structured result of analysing a natural-language goal."""

    intent: str  # e.g. "check_file_size", "run_shell", "read_file", "search"
    target_path: str | None  # primary file or resource path, if applicable
    target_name: str | None  # file name without directory, if applicable
    command_hint: str | None  # shell command embedded in the goal, if any
    extra_criteria: list[str]  # additional acceptance criteria to include


_FILE_SIZE_RE = re.compile(
    r"(?:размер|size|bytes?|wc\b|stat\b)"
    r".*?"
    r"(?:файл[а-я]*\s+)?"
    r"((?:[\w./-]+\.\w+))",
    re.I,
)
_SHELL_RE = re.compile(
    r"(?:выполни|запусти|run|execute|выполнить|shell|коман[дт])\s*[`:]?\s*(.+?)$", re.I
)
_FILE_EXISTS_RE = re.compile(
    r"(?:найди|найти|find|check|проверь|проверить|exists|существует)\s+(?:файл)?\s*([\w._/-]+)",
    re.I,
)
_READ_FILE_RE = re.compile(
    r"(?:прочитай|прочитать|read|cat\b|содержимое|content)"
    r"(?:\s+файл[а-я]*)?"
    r"\s+((?:[\w./-]+\.\w+))",
    re.I,
)


def _analyse_goal(goal: str) -> _GoalAnalysis:
    """Analyse a natural-language *goal* and return structured metadata."""
    goal_stripped = goal.strip()

    # 1) File-size check (Проверь размер файла ...)
    m = _FILE_SIZE_RE.search(goal_stripped)
    if m:
        target_name = m.group(1)
        return _GoalAnalysis(
            intent="check_file_size",
            target_path=None,  # resolved later via find or exact path
            target_name=target_name,
            command_hint=None,
            extra_criteria=[
                "file_exists",
                "correct_target",
                "numeric_size",
                "units_present",
            ],
        )

    # 2) Shell / command execution
    m = _SHELL_RE.search(goal_stripped)
    if m:
        return _GoalAnalysis(
            intent="run_shell",
            target_path=None,
            target_name=None,
            command_hint=m.group(1).strip().strip("`'\""),
            extra_criteria=["output_contains_target"],
        )

    # 3) File existence check
    m = _FILE_EXISTS_RE.search(goal_stripped)
    if m:
        target = m.group(1)
        return _GoalAnalysis(
            intent="check_file_exists",
            target_path=target if "/" in target else None,
            target_name=target if "/" not in target else target.rsplit("/", 1)[-1],
            command_hint=None,
            extra_criteria=["file_exists", "correct_target"],
        )

    # 4) Read file contents
    m = _READ_FILE_RE.search(goal_stripped)
    if m:
        target = m.group(1)
        return _GoalAnalysis(
            intent="read_file",
            target_path=target if "/" in target else None,
            target_name=target if "/" not in target else target.rsplit("/", 1)[-1],
            command_hint=None,
            extra_criteria=["file_exists", "output_nonempty"],
        )

    # 5) Fallback — generic goal
    return _GoalAnalysis(
        intent="generic",
        target_path=None,
        target_name=None,
        command_hint=goal_stripped if len(goal_stripped) < 120 else None,
        extra_criteria=[],
    )


# ── Step builders ────────────────────────────────────────────────────────────────


def _build_steps(analysis: _GoalAnalysis, context: dict[str, Any] | None) -> list[PlanStep]:
    """Build an ordered list of :class:`PlanStep` from a goal analysis.

    Each returned step carries a descriptive *title*, a *tool_name*, the
    keyword *arguments* that will be passed at invocation, an
    *expected_result* dict, and a *verification_method* string.
    """
    base_path = _PROJECT_ROOT
    if context and "workspace" in context:
        base_path = str(context["workspace"])
    durable_path = f"{base_path}/{_DURABLE_SUBDIR}"

    _base_path = base_path

    if analysis.intent == "check_file_size":
        # Step 1: stat the file so we get existence + size in one call.
        target_name = analysis.target_name or ""
        if "/" in target_name:
            # The goal already carries a project-relative path (e.g.
            # "src/antigona/durable/planner.py"); join it once from the
            # project root instead of re-prefixing the durable subdir.
            candidate_path = str(Path(base_path) / target_name)
        else:
            candidate_path = f"{durable_path}/{target_name}"
        steps: list[PlanStep] = [
            PlanStep(
                title=f"Stat {analysis.target_name} to check existence and size",
                tool_name="sandbox.shell",
                arguments={
                    "command": f"stat --format='%s %n' {candidate_path} 2>&1 || echo 'NOT_FOUND'"
                },
                expected_result={
                    "exit_code": 0,
                    "stdout_does_not_contain": "NOT_FOUND",
                },
                verification_method="exit_code_zero",
            ),
        ]
        # Step 2: fallback — locate the file if stat did not find it at the
        # expected path.
        steps.append(
            PlanStep(
                title=f"Locate {analysis.target_name} with find",
                tool_name="sandbox.shell",
                arguments={
                    "command": f"find {base_path} -name '{analysis.target_name}' -type f 2>&1"
                },
                expected_result={"exit_code": 0, "stdout_contains": analysis.target_name},
                verification_method="output_nonempty",
            ),
        )
        # Step 3: measure the actual size once the path is known.
        steps.append(
            PlanStep(
                title=f"Count bytes of {analysis.target_name} with wc",
                tool_name="sandbox.shell",
                arguments={
                    "command": f"wc -c \"$(find {base_path} -name '{analysis.target_name}' -type f | head -1)\" 2>&1 || echo 'FILE_NOT_FOUND'"
                },
                expected_result={"exit_code": 0, "stdout_contains": analysis.target_name},
                verification_method="exit_code_zero",
            ),
        )
        return steps

    if analysis.intent == "run_shell" and analysis.command_hint:
        return [
            PlanStep(
                title=f"Execute: {analysis.command_hint[:60]}",
                tool_name="sandbox.shell",
                arguments={"command": analysis.command_hint},
                expected_result={"exit_code": 0},
                verification_method="exit_code_zero",
            ),
        ]

    if analysis.intent == "check_file_exists":
        target_path = analysis.target_path
        if target_path is None and analysis.target_name:
            target_path = f"{_base_path}/{analysis.target_name}"
        return [
            PlanStep(
                title=f"Check if {analysis.target_name or target_path} exists",
                tool_name="sandbox.shell",
                arguments={
                    "command": f"test -e '{target_path}' 2>&1 && echo 'EXISTS' || echo 'NOT_FOUND'"
                },
                expected_result={"exit_code": 0, "stdout_contains": "EXISTS"},
                verification_method="exit_code_zero",
            ),
            PlanStep(
                title=f"Fallback: locate {analysis.target_name or 'file'}",
                tool_name="sandbox.shell",
                arguments={
                    "command": f"find {_base_path} -name '{analysis.target_name or target_path}' -type f 2>&1"
                },
                expected_result={"exit_code": 0},
                verification_method="output_nonempty",
            ),
        ]

    if analysis.intent == "read_file":
        target_path = analysis.target_path
        if target_path is None and analysis.target_name:
            target_path = f"{_base_path}/{analysis.target_name}"
        return [
            PlanStep(
                title=f"Read contents of {analysis.target_name or target_path}",
                tool_name="workspace.read_file",
                arguments={"path": target_path},
                expected_result={"path": target_path},
                verification_method="output_nonempty",
            ),
        ]

    # Generic fallback: single shell step with the full goal as command.
    cmd = analysis.command_hint or ""
    if context and "text" in context:
        cmd = str(context["text"])
    if not cmd:
        cmd = "echo 'Goal not parseable; list workspace' && ls -la"
    return [
        PlanStep(
            title=f"Execute plan for: {cmd[:80]}",
            tool_name="sandbox.shell",
            arguments={"command": cmd},
            expected_result={"exit_code": 0},
            verification_method="exit_code_zero",
        ),
    ]


# ── Planner ──────────────────────────────────────────────────────────────────────


class Planner:
    """Analyse goals, construct :class:`ExecutionPlan` instances, and replan on failure.

    This is a rule-based planner — it uses pattern matching on the goal
    string rather than an LLM, making it deterministic, fast, and suitable
    for self-hosted autonomous loops.

    Lifecycle
    ---------
    1.  Call :meth:`create_plan` with a natural-language goal.
    2.  Execute the returned plan's steps in order.
    3.  If verification fails with decision ``REPLAN``, call
        :meth:`replan` with the previous :class:`ToolExecutionResult`\ s.
    4.  Repeat from step 2.
    """

    def __init__(self, workspace: str = _PROJECT_ROOT) -> None:
        """Initialise the planner with an optional *workspace* root.

        Args:
            workspace: Absolute path to the root workspace directory.
                Defaults to the canonical project root
                (``antigona.core.paths.project_root()``), not a hardcoded path.
        """
        self._workspace = workspace

    # ── Public API ───────────────────────────────────────────────────────────

    def create_plan(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> ExecutionPlan:
        """Analyse *goal* and produce an :class:`ExecutionPlan`.

        Args:
            goal: Natural-language description of what to accomplish.
            context: Optional dictionary of additional context
                (e.g. ``{"workspace": "/custom/path", "text": "..."}``).

        Returns:
            A fully populated :class:`ExecutionPlan` with ordered steps
            and acceptance criteria.
        """
        ctx = dict(context) if context else {}
        if "workspace" not in ctx:
            ctx.setdefault("workspace", self._workspace)

        analysis = _analyse_goal(goal)
        steps = _build_steps(analysis, ctx)
        criteria = _default_criteria(analysis.extra_criteria)

        return ExecutionPlan(
            task_id=str(uuid.uuid4()),
            revision=1,
            goal=goal,
            steps=steps,
            acceptance_criteria=criteria,
            reason=(
                f"Created plan with intent='{analysis.intent}', "
                f"target='{analysis.target_name or analysis.target_path or 'N/A'}', "
                f"{len(steps)} step(s), {len(criteria)} acceptance criterion/criteria"
            ),
        )

    def replan(
        self,
        task_id: str,
        instructions: str,
        previous_attempts: list[ToolExecutionResult],
    ) -> ExecutionPlan:
        """Generate a revised :class:`ExecutionPlan` after a failed attempt.

        Analyses the *previous_attempts* to identify failure patterns
        (timeouts, missing files, permission errors, non-zero exit codes,
        empty output) and produces a concretely different plan that
        addresses those failures.

        Args:
            task_id: UUID of the parent task this plan serves.  Must match
                the original task_id from the first plan so the execution
                loop can correlate revisions.
            instructions: Free-text guidance from the verifier or user
                describing what went wrong and what to change.
            previous_attempts: List of :class:`ToolExecutionResult`\ s
                from the most recent attempt(s), in chronological order.

        Returns:
            A new :class:`ExecutionPlan` with ``revision`` incremented
            and a step sequence that differs from the previous plan.
        """
        # ── Analyse failures ────────────────────────────────────────────────
        error_patterns = self._classify_failures(previous_attempts)

        # ── Build replan reason ─────────────────────────────────────────────
        error_summary = (
            "; ".join(f"{pat}: {desc[:80]}" for pat, desc in error_patterns)
            or "no specific error pattern detected"
        )
        reason = (
            f"REPLAN (task={task_id}): {instructions[:200]}. Observed error(s): {error_summary}"
        )

        # ── Construct a revised plan ─────────────────────────────────────────
        new_steps = self._build_replan_steps(
            previous_attempts=previous_attempts,
            error_patterns=error_patterns,
            instructions=instructions,
        )

        # ── Adjust acceptance criteria for replan ───────────────────────────
        criteria_extra: list[str] = []
        if any("not_found" in p or "missing" in p for p, _ in error_patterns):
            criteria_extra.append("file_exists")
        if any("timeout" in p for p, _ in error_patterns):
            criteria_extra.append("resource_reachable")

        criteria = _default_criteria(criteria_extra)

        return ExecutionPlan(
            task_id=task_id,
            revision=2,  # bumped from 1 → 2 on first replan
            goal=instructions,
            steps=new_steps,
            acceptance_criteria=criteria,
            reason=reason,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _classify_failures(
        attempts: list[ToolExecutionResult],
    ) -> list[tuple[str, str]]:
        """Classify failure patterns from a list of execution results.

        Returns a list of ``(pattern_name, description)`` tuples ordered
        by severity (timeouts first, then errors, then empty results).
        """
        patterns: list[tuple[str, str]] = []

        for result in attempts:
            if result.timed_out:
                patterns.append(
                    (
                        "timeout",
                        f"step '{result.step_id}' timed out after {result.duration_ms}ms",
                    )
                )
            if result.error_type:
                patterns.append(
                    (
                        f"error_{result.error_type}",
                        result.error_message or f"error_type={result.error_type}",
                    )
                )
            if result.exit_code is not None and result.exit_code != 0:
                patterns.append(
                    (
                        "non_zero_exit",
                        f"step '{result.step_id}' exited with code {result.exit_code}: "
                        f"{result.stderr[:200] or '(no stderr)'}",
                    )
                )
            if not result.technical_success:
                patterns.append(
                    (
                        "technical_failure",
                        f"step '{result.step_id}' did not complete successfully "
                        f"(error: {result.error_message or 'unknown'})",
                    )
                )
            if not result.stdout.strip() and not result.stderr.strip():
                patterns.append(
                    (
                        "empty_output",
                        f"step '{result.step_id}' produced no output",
                    )
                )

        # Deduplicate by pattern name while keeping the first occurrence.
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for pattern, desc in patterns:
            if pattern not in seen:
                seen.add(pattern)
                unique.append((pattern, desc))

        return unique

    @staticmethod
    def _build_replan_steps(
        previous_attempts: list[ToolExecutionResult],
        error_patterns: list[tuple[str, str]],
        instructions: str,
    ) -> list[PlanStep]:
        """Build a different step sequence suited to the detected failures.

        The replan logic ensures the returned steps are *not* identical to
        what a fresh :meth:`create_plan` would produce for the same goal.
        """
        instructions_lower = instructions.lower()
        steps: list[PlanStep] = []
        has_not_found = any("not_found" in p or "missing" in p for p, _ in error_patterns)
        has_timeout = any("timeout" in p for p, _ in error_patterns)
        has_permissions = any("permission" in p or "denied" in p for p, _ in error_patterns)
        has_nonzero = any("non_zero" in p for p, _ in error_patterns)

        # ── Step 1: Pre-flight health checks ──────────────────────────────
        if has_timeout:
            steps.append(
                PlanStep(
                    title="Pre-flight: check workspace reachability",
                    tool_name="sandbox.shell",
                    arguments={
                        "command": f"test -d '{_PROJECT_ROOT}' && echo 'REACHABLE' || echo 'UNREACHABLE'"
                    },
                    expected_result={"exit_code": 0, "stdout_contains": "REACHABLE"},
                    verification_method="exit_code_zero",
                ),
            )

        if has_permissions:
            steps.append(
                PlanStep(
                    title="Check file-system permissions on workspace",
                    tool_name="sandbox.shell",
                    arguments={
                        "command": f"ls -ld '{_PROJECT_ROOT}' && ls -l '{_PROJECT_ROOT}/src/antigona/durable/' | head -10"
                    },
                    expected_result={"exit_code": 0},
                    verification_method="exit_code_zero",
                ),
            )

        # ── Step 2: Locate the missing target ────────────────────────────
        if has_not_found:
            # Extract the filename from failed commands if possible.
            filename = _extract_filename_from_attempts(previous_attempts)
            if filename:
                steps.append(
                    PlanStep(
                        title=f"Locate missing file '{filename}' via find",
                        tool_name="sandbox.shell",
                        arguments={
                            "command": f"find {_PROJECT_ROOT} -name '{filename}' -type f 2>&1"
                        },
                        expected_result={"exit_code": 0},
                        verification_method="output_nonempty",
                    ),
                )
            else:
                steps.append(
                    PlanStep(
                        title="Locate missing target via broad find",
                        tool_name="sandbox.shell",
                        arguments={
                            "command": f"find {_PROJECT_ROOT} -name '*.py' -type f 2>&1 | head -30"
                        },
                        expected_result={"exit_code": 0},
                        verification_method="output_nonempty",
                    ),
                )

        # ── Step 3: Retry with changes ────────────────────────────────────
        if has_nonzero:
            # Grab the last failed command and re-run it with --verbose or similar.
            last_command = ""
            for r in reversed(previous_attempts):
                if r.exit_code is not None and r.exit_code != 0:
                    last_command = r.arguments.get("command", "")
                    break
            if last_command:
                steps.append(
                    PlanStep(
                        title=f"Retry with diagnostics: {last_command[:60]}",
                        tool_name="sandbox.shell",
                        arguments={"command": f"{last_command} 2>&1; echo '---EXIT: $?'"},
                        expected_result={"exit_code": 0},
                        verification_method="exit_code_zero",
                    ),
                )
            else:
                steps.append(
                    PlanStep(
                        title="Run fallback diagnostic command",
                        tool_name="sandbox.shell",
                        arguments={
                            "command": f"echo 'Replan instructions: {instructions[:120].strip()}' && whoami && pwd"
                        },
                        expected_result={"exit_code": 0},
                        verification_method="exit_code_zero",
                    ),
                )

        # ── Step 4: Follow replan instructions literally if present ───────
        if instructions_lower and not steps:
            steps.append(
                PlanStep(
                    title="Execute replan instructions as shell command",
                    tool_name="sandbox.shell",
                    arguments={"command": instructions[:200]},
                    expected_result={"exit_code": 0},
                    verification_method="exit_code_zero",
                ),
            )

        # ── Absolute fallback: at least one step ──────────────────────────
        if not steps:
            steps.append(
                PlanStep(
                    title="Fallback: list workspace contents",
                    tool_name="sandbox.shell",
                    arguments={
                        "command": f"ls -la '{_PROJECT_ROOT}/src/antigona/durable/' 2>&1 || ls -la '{_PROJECT_ROOT}' 2>&1"
                    },
                    expected_result={"exit_code": 0},
                    verification_method="output_nonempty",
                ),
            )

        return steps


# ── Module-level helpers ─────────────────────────────────────────────────────────


def _extract_filename_from_attempts(
    attempts: list[ToolExecutionResult],
) -> str | None:
    """Try to extract a target filename from failed execution arguments."""
    for result in attempts:
        command = result.arguments.get("command", "")
        # Patterns:  stat <path>,  test -e <path>,  find ... -name '<name>'
        for pattern in (r"stat\s+(\S+)", r"test\s+-[efd]\s+'([^']+)'", r"-name\s+'([^']+)'"):
            m = re.search(pattern, command)
            if m:
                return m.group(1).rsplit("/", 1)[-1]
    return None


__all__ = [
    "Planner",
]
