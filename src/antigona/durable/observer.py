"""Observation layer — distil a raw :class:`ToolExecutionResult` into
actionable signals for the verification loop.

The Observer inspects every aspect of a tool-invocation result — exit code,
stdout, stderr, expected output targets — and produces a structured
:class:`Observation` with retryability, suggested action, and confidence.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from antigona.durable.execution_models import (
    Observation,
    PlanStep,
    ToolExecutionResult,
)

# ── Temporary / retryable error patterns (stderr regex) ───────────────────────

_TEMPORARY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"timed?\s*out", re.IGNORECASE),
    re.compile(r"connection\s+refused", re.IGNORECASE),
    re.compile(r"connection\s+reset", re.IGNORECASE),
    re.compile(r"connection\s+timed?\s*out", re.IGNORECASE),
    re.compile(r"network\s+is\s+unreachable", re.IGNORECASE),
    re.compile(r"no\s+route\s+to\s+host", re.IGNORECASE),
    re.compile(r"name\s+or\s+service\s+not\s+known", re.IGNORECASE),
    re.compile(r"resource\s+(temporarily\s+)?unavailable", re.IGNORECASE),
    re.compile(r"resource\s+is\s+busy", re.IGNORECASE),
    re.compile(r"too\s+many\s+open\s+files", re.IGNORECASE),
    re.compile(r"rate\s+limit", re.IGNORECASE),
    re.compile(r"retry\s+later", re.IGNORECASE),
    re.compile(r"service\s+unavailable", re.IGNORECASE),
    re.compile(r"5\d{2}\s+server\s+error", re.IGNORECASE),
    re.compile(r"internal\s+server\s+error", re.IGNORECASE),
    re.compile(r"bad\s+gateway", re.IGNORECASE),
    re.compile(r"gateway\s+timeout", re.IGNORECASE),
]

# ── Patterns that indicate a permanent / logical error — NOT retryable ────────

_PERMANENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"permission\s+denied", re.IGNORECASE),
    re.compile(r"no\s+such\s+file", re.IGNORECASE),
    re.compile(r"file\s+not\s+found", re.IGNORECASE),
    re.compile(r"invalid\s+argument", re.IGNORECASE),
    re.compile(r"command\s+not\s+found", re.IGNORECASE),
    re.compile(r"not\s+a\s+valid", re.IGNORECASE),
    re.compile(r"does\s+not\s+exist", re.IGNORECASE),
    re.compile(r"syntax\s+error", re.IGNORECASE),
    re.compile(r"undefined\s+(symbol|reference)", re.IGNORECASE),
    re.compile(r"import\s+error", re.IGNORECASE),
    re.compile(r"module\s+not\s+found", re.IGNORECASE),
    re.compile(r"type\s+mismatch", re.IGNORECASE),
]

# ── Observer ─────────────────────────────────────────────────────────────────


class Observer:
    """Interprets :class:`ToolExecutionResult` objects into structured
    :class:`Observation` instances.

    The observer is stateless by design — all context it needs is passed
    explicitly via ``observe()``.  This keeps it testable and idempotent.
    """

    # ── Public API ──────────────────────────────────────────────────────────

    def observe(
        self,
        task_id: str,
        step: PlanStep,
        result: ToolExecutionResult,
        *,
        previous_attempts: list[Observation] | None = None,
    ) -> Observation:
        """Analyse *result* and produce an :class:`Observation`.

        Parameters
        ----------
        task_id:
            UUID of the parent task (included for traceability).
        step:
            The :class:`PlanStep` that was executed.
        result:
            The raw execution result to analyse.
        previous_attempts:
            Observations from prior attempts of the same step, if any.
            Used to detect repeated errors.

        Returns
        -------
        Observation
            A structured interpretation of the result.
        """
        step_id = step.step_id
        attempt = step.attempt_count

        # ---- 1. Exit code ---------------------------------------------------
        exit_code = result.exit_code
        exit_code_success = exit_code is not None and exit_code == 0
        technical_success = result.technical_success and exit_code_success

        # ---- 2. Stdout ------------------------------------------------------
        stdout = result.stdout or ""
        stdout_empty = not stdout.strip()
        # Poor man's "contains expected result" — a future version should
        # use the step's verification_method / expected_result content.
        expected_result_found = self._check_expected_in_output(
            stdout,
            step.expected_result,
        )

        # ---- 3. Stderr analysis ---------------------------------------------
        stderr = result.stderr or ""
        errors: list[str] = []
        warnings: list[str] = []
        if stderr.strip():
            lines = stderr.strip().splitlines()
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                if self._looks_like_error(stripped):
                    errors.append(stripped)
                else:
                    warnings.append(stripped)

        # ---- 4. Target existence --------------------------------------------
        target_exists: bool | None = self._check_target_exists(
            step.expected_result,
        )

        # ---- 5. Repeated-error fingerprint ----------------------------------
        error_fingerprint = self._compute_fingerprint(result)
        repeated = self._detect_repeated_error(
            error_fingerprint,
            previous_attempts or [],
        )

        # ---- 6. Retryable? --------------------------------------------------
        is_temporary = self._is_temporary_error(stderr, exit_code)
        is_permanent = self._is_permanent_error(stderr, exit_code)
        retryable = (result.timed_out or result.sandboxed or is_temporary) and not is_permanent

        # ---- 7. Suggested action --------------------------------------------
        suggested_action = self._suggest_action(
            technical_success=technical_success,
            stdout_empty=stdout_empty,
            expected_result_found=expected_result_found,
            target_exists=target_exists,
            retryable=retryable,
            repeated=repeated,
            has_errors=bool(errors),
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            attempt=attempt,
            step=step,
        )

        # ---- 8. Confidence --------------------------------------------------
        confidence = self._compute_confidence(
            technical_success=technical_success,
            stdout_empty=stdout_empty,
            expected_result_found=expected_result_found,
            target_exists=target_exists,
            has_errors=bool(errors),
            timed_out=result.timed_out,
        )

        # ---- 9. Summary -----------------------------------------------------
        facts: list[str] = []
        if technical_success:
            facts.append("Tool returned exit code 0 (technical success).")
        if not stdout_empty:
            facts.append(f"Stdout produced ({len(stdout.strip())} chars).")
        if result.artifacts:
            facts.append(f"Produced {len(result.artifacts)} artifact(s).")

        summary_parts: list[str] = []
        if result.timed_out:
            summary_parts.append("Execution timed out.")
        if result.cancelled:
            summary_parts.append("Execution was cancelled.")
        if errors:
            summary_parts.append(f"Detected {len(errors)} stderr error(s).")
        if warnings:
            summary_parts.append(f"Detected {len(warnings)} stderr warning(s).")
        if repeated:
            summary_parts.append("Error pattern matches a previous attempt.")
        if target_exists is True:
            summary_parts.append("Expected target file exists.")
        elif target_exists is False:
            summary_parts.append("Expected target file does NOT exist.")
        if expected_result_found:
            summary_parts.append("Expected content found in output.")
        if not stdout_empty and not expected_result_found:
            summary_parts.append("Output produced but expected content not matched.")
        if not summary_parts:
            summary_parts.append("No salient signals detected.")

        summary = "; ".join(summary_parts)

        return Observation(
            step_id=step_id,
            attempt=attempt,
            summary=summary,
            facts=facts,
            errors=errors,
            warnings=warnings,
            output_empty=stdout_empty,
            target_exists=target_exists,
            expected_result_found=expected_result_found,
            artifacts_valid=not result.artifacts or True,  # no deep validation yet
            retryable=retryable,
            suggested_action=suggested_action,
            confidence=confidence,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_temporary_error(stderr: str, exit_code: int | None) -> bool:
        """Return *True* if *stderr* matches a transient-failure pattern.

        Matches include timeouts, network errors, resource-unavailable,
        rate-limiting, and server-side 5xx errors.
        """
        if exit_code is not None and exit_code in (1, 2, 126, 127):
            # Common non-retryable exit codes for logical errors.
            return False
        if not stderr:
            return False
        for pattern in _TEMPORARY_PATTERNS:
            if pattern.search(stderr):
                return True
        return False

    @staticmethod
    def _is_permanent_error(stderr: str, exit_code: int | None) -> bool:
        """Return *True* if *stderr* matches a permanent/logical-error pattern."""
        if not stderr:
            return False
        for pattern in _PERMANENT_PATTERNS:
            if pattern.search(stderr):
                return True
        return False

    @staticmethod
    def _detect_repeated_error(
        error_fingerprint: str,
        previous_attempts: list[Observation],
    ) -> bool:
        """Return *True* if *error_fingerprint* matches a prior observation.

        Uses a simple fingerprint stored in the observation's errors list.
        An empty fingerprint never counts as repeated.
        """
        if not error_fingerprint:
            return False
        for prev in previous_attempts:
            for err in prev.errors:
                fp = hashlib.sha256(err.encode("utf-8")).hexdigest()
                if fp == error_fingerprint:
                    return True
        return False

    @staticmethod
    def _summarize(text: str, max_len: int = 200) -> str:
        """Truncate *text* to *max_len* characters, appending ``…`` if cut."""
        if len(text) <= max_len:
            return text
        # Try to break at a word boundary.
        truncated = text[:max_len]
        last_space = truncated.rfind(" ")
        if last_space > max_len // 2:
            truncated = truncated[:last_space]
        return f"{truncated}…"

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _is_redaction_or_diagnostic_only(stdout: str) -> bool:
        """Return True if *stdout* contains only whitespace, redactions, or diagnostics."""
        stripped = stdout.strip()
        if not stripped:
            return True

        # Redaction placeholders (e.g. [REDACTED], ***, ---, ..., <redacted>, [filtered], *** REDACTED ***)
        without_redactions = re.sub(
            r"\[?\b(?:REDACTED|redacted|FILTERED|filtered)\b\]?|\*{3,}|-{3,}|\.{3,}|<[^>]*redacted[^>]*>",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()
        if not without_redactions:
            return True

        # Check if every non-empty line is a diagnostic prefix / banner
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if not lines:
            return True

        diagnostic_prefix = re.compile(
            r"^(?:\[?(?:DEBUG|TRACE|INFO|DIAG|DIAGNOSTIC)\]?:?|={3,}|-{3,}|>{3,})\b",
            re.IGNORECASE,
        )
        return all(diagnostic_prefix.match(line) for line in lines)

    @staticmethod
    def _check_expected_in_output(
        stdout: str,
        expected: dict[str, Any],
    ) -> bool:
        """Strict check for expected content in *stdout*.

        Enforces proof-over-assertion (EVD-001):
        - Success requires an explicit expected criterion.
        - Non-empty arbitrary stdout, generic 'OK', diagnostic-only,
          whitespace-only, or redaction-only stdout must NEVER establish
          expected_result_found without an explicit matching criterion.
        - Supports:
            * 'contains' / 'stdout_contains'
            * 'exact' / 'stdout_exact' / 'equals' / 'stdout_equals'
            * 'regex' / 'pattern' / 'stdout_regex' / 'stdout_pattern'
            * 'stdout_does_not_contain' / 'does_not_contain'
        - When no stdout criteria are specified, returns False (fail closed).
        """
        if not stdout or not stdout.strip():
            return False

        if not expected:
            return False

        # 1. Collect positive criteria
        contains_val: str | None = expected.get("contains") or expected.get("stdout_contains")
        exact_val: Any = (
            expected.get("exact")
            if "exact" in expected
            else expected.get("stdout_exact")
            if "stdout_exact" in expected
            else expected.get("equals")
            if "equals" in expected
            else expected.get("stdout_equals")
        )
        regex_val: str | None = (
            expected.get("regex")
            or expected.get("pattern")
            or expected.get("stdout_regex")
            or expected.get("stdout_pattern")
        )

        # 2. Collect negative criteria
        does_not_contain_val: str | None = expected.get("stdout_does_not_contain") or expected.get(
            "does_not_contain"
        )

        has_positive_criterion = (
            contains_val is not None or exact_val is not None or regex_val is not None
        )
        has_negative_criterion = does_not_contain_val is not None

        # Fail closed if no stdout criteria are present
        if not has_positive_criterion and not has_negative_criterion:
            return False

        # Evaluate negative criterion first if present
        if does_not_contain_val is not None:
            if not isinstance(does_not_contain_val, str) or not does_not_contain_val:
                return False
            if does_not_contain_val in stdout:
                return False

        # Evaluate positive criteria
        if has_positive_criterion:
            if contains_val is not None:
                if not isinstance(contains_val, str) or not contains_val:
                    return False
                if contains_val not in stdout:
                    return False

            if exact_val is not None:
                expected_str = str(exact_val)
                if stdout.strip() != expected_str.strip():
                    return False

            if regex_val is not None:
                if not isinstance(regex_val, str) or not regex_val:
                    return False
                try:
                    if not re.search(regex_val, stdout):
                        return False
                except re.error:
                    return False

            return True

        # If only negative criterion was specified, ensure stdout is not just redaction or diagnostics
        if has_negative_criterion:
            if Observer._is_redaction_or_diagnostic_only(stdout):
                return False
            return True

        return False

    @staticmethod
    def _check_target_exists(expected: dict[str, Any]) -> bool | None:
        """Check whether the expected output target file/resource exists.

        Returns ``True`` / ``False`` if ``expected["path"]`` is set,
        or ``None`` when no path is specified.
        """
        path: str | None = expected.get("path")
        if not path:
            return None
        return os.path.isfile(path)

    @staticmethod
    def _looks_like_error(line: str) -> bool:
        """Heuristic: does *line* look like an error message?"""
        lower = line.lower()
        error_triggers = (
            "error",
            "fatal",
            "exception",
            "traceback",
            "fail",
            "abort",
            "panic",
            "cannot",
        )
        if any(t in lower for t in error_triggers):
            return True
        return False

    @staticmethod
    def _compute_fingerprint(result: ToolExecutionResult) -> str:
        """Produce a SHA-256 hex digest of the result's stderr or error_message.

        Returns empty string when there is no stderr or error_message to
        fingerprint.
        """
        raw = (result.stderr or "") or (result.error_message or "")
        if not raw.strip():
            return ""
        # Normalise whitespace before hashing.
        normalised = re.sub(r"\s+", " ", raw.strip())
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    @staticmethod
    def _suggest_action(
        *,
        technical_success: bool,
        stdout_empty: bool,
        expected_result_found: bool,
        target_exists: bool | None,
        retryable: bool,
        repeated: bool,
        has_errors: bool,
        timed_out: bool,
        cancelled: bool,
        attempt: int,
        step: PlanStep,
    ) -> str:
        """Determine the recommended next action for the execution loop."""
        if cancelled:
            return "abort"
        if timed_out and retryable and attempt < step.max_attempts:
            return "retry"
        if technical_success and expected_result_found:
            if target_exists is False:
                return "replan"
            return "proceed"
        if technical_success and not stdout_empty and not expected_result_found:
            if repeated and retryable:
                return "replan"
            return "retry"
        if technical_success and stdout_empty:
            if retryable:
                return "retry"
            return "replan"
        if not technical_success:
            if retryable and not repeated and attempt < step.max_attempts:
                return "retry"
            if retryable and repeated:
                return "replan"
            return "wait_user"
        if has_errors and retryable:
            return "retry"
        return "wait_user"

    @staticmethod
    def _compute_confidence(
        *,
        technical_success: bool,
        stdout_empty: bool,
        expected_result_found: bool,
        target_exists: bool | None,
        has_errors: bool,
        timed_out: bool,
    ) -> float:
        """Compute a confidence score in [0.0, 1.0]."""
        score = 0.5  # neutral baseline

        if technical_success:
            score += 0.2
        else:
            score -= 0.2

        if timed_out:
            score -= 0.2

        if expected_result_found:
            score += 0.2
        elif not stdout_empty and not expected_result_found:
            score -= 0.1

        if target_exists is True:
            score += 0.1
        elif target_exists is False:
            score -= 0.1

        if has_errors:
            score -= 0.1

        return max(0.0, min(1.0, score))


# ── Module-level re-exports ──────────────────────────────────────────────

__all__ = [
    "Observer",
]
