"""Regression and contract tests for EVD-001 (Observer evidence validation).

Guarantees:
1. Proof-over-assertion: success requires an explicit verifier / expected criterion
   or structurally valid evidence.
2. Non-empty arbitrary stdout, generic 'OK', diagnostic-only, whitespace-only,
   or redaction-only stdout must NEVER establish PASS / expected_result_found.
3. Explicit valid criteria (contains, stdout_contains, exact, regex, negative criteria)
   continue to function deterministically.
"""

from __future__ import annotations

from antigona.durable.execution_models import (
    ExecutionPlan,
    PlanStep,
    ToolExecutionResult,
)
from antigona.durable.observer import Observer
from antigona.durable.verifier import Verifier


def _make_step(
    expected_result: dict[str, object] | None = None,
    verification_method: str = "default",
    title: str = "Test step",
) -> PlanStep:
    return PlanStep(
        step_id="step-1",
        title=title,
        tool_name="sandbox.shell",
        arguments={"command": "echo test"},
        expected_result=expected_result or {},
        verification_method=verification_method,
    )


def _make_result(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    technical_success: bool = True,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        task_id="task-1",
        step_id="step-1",
        tool_name="sandbox.shell",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        technical_success=technical_success,
    )


class TestObserverEvidenceContract:
    """Test suite for Observer expected-result evidence validation (EVD-001)."""

    # ── Red regression proofs against arbitrary fallback ─────────────────────

    def test_evd001_arbitrary_stdout_fallback_rejected(self) -> None:
        """Arbitrary non-empty stdout without stdout expectation must not produce expected_result_found."""
        observer = Observer()
        step = _make_step(expected_result={"exit_code": 0})
        result = _make_result(stdout="arbitrary output from unverified tool")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False
        assert "Expected content found in output." not in obs.summary

    def test_evd001_generic_ok_stdout_without_explicit_contract_rejected(self) -> None:
        """Generic 'OK' string without an explicit expectation must not satisfy expected_result_found."""
        observer = Observer()
        step = _make_step(expected_result={"exit_code": 0})
        result = _make_result(stdout="OK\n")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False

    def test_evd001_redaction_only_stdout_rejected(self) -> None:
        """Redaction markers alone must never establish expected_result_found."""
        observer = Observer()
        redaction_samples = [
            "[REDACTED]",
            "*** REDACTED ***",
            "---",
            "...",
            "[filtered]",
            "<redacted>",
            "   [REDACTED]\n***   ",
        ]
        for sample in redaction_samples:
            step = _make_step(expected_result={"stdout_does_not_contain": "ERROR"})
            result = _make_result(stdout=sample)
            obs = observer.observe("task-1", step, result)
            assert obs.expected_result_found is False, f"Failed closed for sample: {sample!r}"

    def test_evd001_diagnostic_only_stdout_rejected(self) -> None:
        """Diagnostic headers alone without payload must not satisfy negative-only criterion."""
        observer = Observer()
        diagnostic_stdout = (
            "DEBUG: initializing subsystem\n"
            "INFO: connection pool warmed\n"
            "[TRACE] step trace 0x1234\n"
        )
        step = _make_step(expected_result={"stdout_does_not_contain": "FATAL"})
        result = _make_result(stdout=diagnostic_stdout)

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False

    def test_evd001_whitespace_only_stdout_rejected(self) -> None:
        """Whitespace-only output must never establish expected_result_found."""
        observer = Observer()
        step = _make_step(expected_result={"contains": "something"})
        result = _make_result(stdout="   \t\n   ")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False
        assert obs.output_empty is True

    def test_evd001_empty_expected_result_fails_closed(self) -> None:
        """An empty expected_result dictionary must fail closed."""
        observer = Observer()
        step = _make_step(expected_result={})
        result = _make_result(stdout="Valid looking output")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False

    def test_evd001_path_only_expected_result_does_not_falsely_match_stdout(self) -> None:
        """When expected_result has only 'path', stdout matching must not trigger."""
        observer = Observer()
        step = _make_step(expected_result={"path": "/nonexistent/test.txt"})
        result = _make_result(stdout="Wrote 42 bytes to /nonexistent/test.txt")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False
        assert obs.target_exists is False

    # ── Green proofs for explicit expected criteria ──────────────────────────

    def test_evd001_contains_match_succeeds(self) -> None:
        """Explicit 'contains' key matching stdout must return expected_result_found=True."""
        observer = Observer()
        step = _make_step(expected_result={"contains": "HASH: 9a8b7c"})
        result = _make_result(stdout="Process finished. HASH: 9a8b7c computed.")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is True
        assert obs.suggested_action == "proceed"
        assert "Expected content found in output." in obs.summary

    def test_evd001_contains_mismatch_fails_closed(self) -> None:
        """Explicit 'contains' key missing from stdout must return expected_result_found=False."""
        observer = Observer()
        step = _make_step(expected_result={"contains": "HASH: 9a8b7c"})
        result = _make_result(stdout="Process finished. HASH: 112233 computed.")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False
        assert obs.suggested_action == "retry"

    def test_evd001_stdout_contains_match_succeeds(self) -> None:
        """Explicit 'stdout_contains' key matching stdout must return expected_result_found=True."""
        observer = Observer()
        step = _make_step(expected_result={"exit_code": 0, "stdout_contains": "EXISTS"})
        result = _make_result(stdout="Status check: EXISTS")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is True
        assert obs.suggested_action == "proceed"

    def test_evd001_stdout_contains_mismatch_fails_closed(self) -> None:
        """Explicit 'stdout_contains' missing from stdout must return expected_result_found=False."""
        observer = Observer()
        step = _make_step(expected_result={"exit_code": 0, "stdout_contains": "EXISTS"})
        result = _make_result(stdout="Status check: NOT_FOUND")

        obs = observer.observe("task-1", step, result)

        assert obs.expected_result_found is False

    def test_evd001_exact_match_semantics(self) -> None:
        """Explicit 'exact' / 'stdout_exact' match semantics."""
        observer = Observer()
        step_exact = _make_step(expected_result={"exact": "42"})

        res_pass = _make_result(stdout="42\n")
        assert observer.observe("task-1", step_exact, res_pass).expected_result_found is True

        res_fail = _make_result(stdout="420\n")
        assert observer.observe("task-1", step_exact, res_fail).expected_result_found is False

    def test_evd001_regex_match_semantics(self) -> None:
        """Explicit 'regex' / 'pattern' matching."""
        observer = Observer()
        step_regex = _make_step(expected_result={"regex": r"Total:\s+\d+\s+items"})

        res_pass = _make_result(stdout="Report generated. Total: 15 items found.")
        assert observer.observe("task-1", step_regex, res_pass).expected_result_found is True

        res_fail = _make_result(stdout="Report generated. Total: none found.")
        assert observer.observe("task-1", step_regex, res_fail).expected_result_found is False

    def test_evd001_negative_criterion_semantics(self) -> None:
        """Explicit 'stdout_does_not_contain' / 'does_not_contain' criterion."""
        observer = Observer()
        step_neg = _make_step(expected_result={"stdout_does_not_contain": "NOT_FOUND"})

        res_pass = _make_result(stdout="File size: 2048 bytes; path: /var/log/app.log")
        assert observer.observe("task-1", step_neg, res_pass).expected_result_found is True

        res_fail = _make_result(stdout="File check: NOT_FOUND")
        assert observer.observe("task-1", step_neg, res_fail).expected_result_found is False

    def test_evd001_combined_positive_and_negative_criteria(self) -> None:
        """Combined positive and negative criteria."""
        observer = Observer()
        step_combo = _make_step(
            expected_result={
                "stdout_contains": "READY",
                "stdout_does_not_contain": "DEGRADED",
            }
        )

        res_pass = _make_result(stdout="System state: READY (all nodes healthy)")
        assert observer.observe("task-1", step_combo, res_pass).expected_result_found is True

        res_fail = _make_result(stdout="System state: READY (cluster DEGRADED)")
        assert observer.observe("task-1", step_combo, res_fail).expected_result_found is False

    # ── End-to-End Verifier integration ──────────────────────────────────────

    def test_evd001_verifier_integration_prevents_false_pass_done(self) -> None:
        """Unverified step output without explicit criteria must replan rather than pass."""
        observer = Observer()
        step = _make_step(
            title="Execute arbitrary command",
            expected_result={"exit_code": 0},
        )
        result = _make_result(stdout="SyntaxError: invalid syntax near line 4")
        plan = ExecutionPlan(steps=[step], acceptance_criteria=[])

        obs = observer.observe("task-1", step, result)
        assert obs.expected_result_found is False

        verdict = Verifier.verify_step(
            task="Perform syntax check",
            plan=plan,
            step=step,
            result=result,
            observation=obs,
        )

        # Must fail closed: REPLAN instead of PASS
        assert verdict.decision == "REPLAN"
        assert "expected result was not found" in verdict.reason
