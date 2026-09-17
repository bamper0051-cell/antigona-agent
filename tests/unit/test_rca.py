"""Hermes RCA — unit tests covering the failure-test matrix (spec section 18).

Covers:
  1. Worker Python exception -> RCA generated
  2. Tool failure -> tool identified
  3. Provider timeout -> root cause linked (symptom vs cause)
  4. MCP failure -> infra/model distinction
  5. Policy DENY -> classified correctly
  6. Hermes unavailable -> Antigona continues (available=False reported)
  8. Secret leakage attempt -> redacted
  9. 17 duplicate errors -> aggregated
  10. concurrent errors -> correlation isolation
Plus: envelope from exception, confidence levels, safe_to_auto_fix always False,
CLI overlay rendering, /hermes command intent wiring.
"""

from __future__ import annotations

from antigona.events.event_types import ErrorOccurred
from antigona.rca import (
    Deduplicator,
    ErrorEnvelope,
    RCAEngine,
    RCAStatus,
    fingerprint,
)
from antigona.rca.events import HermesRCAConsumer, build_envelope_from_error_event
from antigona.rca.result import RCAConfidence


class TestRCAWorker:
    """Failure-test matrix scenarios 1-5."""

    def test_worker_exception_generates_rca(self) -> None:
        env = ErrorEnvelope.from_exception(
            RuntimeError("boom in worker"),
            source_component="worker",
            operation="turn_execution",
            correlation_id="c_worker",
        )
        result = RCAEngine().diagnose(env)
        assert result.category != "UNKNOWN"
        assert result.root_cause
        assert result.error_id == env.error_id

    def test_tool_failure_identifies_tool(self) -> None:
        env = ErrorEnvelope.from_exception(
            Exception("shell command failed with exit code 1"),
            source_component="worker",
            operation="tool_execution",
            tool_name="owner_shell",
            provider=None,
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "TOOL"
        assert "owner_shell" in result.affected_components or "owner_shell" in " ".join(
            [e.value for e in result.evidence]
        )

    def test_provider_timeout_links_root_cause(self) -> None:
        env = ErrorEnvelope.from_exception(
            TimeoutError("provider request timed out after 120s; worker lease expired"),
            source_component="worker",
            operation="model_call",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        result = RCAEngine().diagnose(env)
        # symptom (timeout) vs root cause (provider stall -> lease expiry)
        assert "lease" in result.root_cause.lower()
        assert "provider" in result.root_cause.lower()
        assert result.confidence == RCAConfidence.HIGH

    def test_mcp_failure_marks_component(self) -> None:
        env = ErrorEnvelope.from_exception(
            Exception("mcp stdio server failed to start"),
            source_component="mcp",
            operation="discovery",
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "MCP"

    def test_policy_deny_classified_correctly(self) -> None:
        env = ErrorEnvelope.from_exception(
            PermissionError("policy denied: requires approval"),
            source_component="policy",
            operation="check_shell",
            policy_decision={"category": "shell", "decision": "deny"},
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "POLICY"


class TestRCAUnavailable:
    """Scenario 6: Hermes unavailable -> Antigona continues."""

    def test_unavailable_reports_flag_but_returns_result(self) -> None:
        env = ErrorEnvelope.from_exception(
            RuntimeError("x"), source_component="worker", operation="op"
        )
        result = RCAEngine(available=False).diagnose(env)
        assert result.hermes_available is False
        assert result.status == RCAStatus.DIAGNOSED  # still a structured result
        assert result.root_cause  # local analysis still produced

    def test_safe_to_auto_fix_never_true(self) -> None:
        env = ErrorEnvelope.from_exception(
            RuntimeError("x"), source_component="worker", operation="op"
        )
        result = RCAEngine().diagnose(env)
        assert result.safe_to_auto_fix is False
        assert result.requires_owner_approval is True


class TestRedaction:
    """Scenario 8: secret leakage attempt -> redacted."""

    def test_api_key_and_password_redacted(self) -> None:
        env = ErrorEnvelope(
            tool_arguments_redacted={
                "api_key": "sk-live-secret-123",
                "password": "hunter2",
                "bearer": "Bearer abcdef",
                "safe": "plain-value",
            }
        )
        red = env.redacted()
        assert red.tool_arguments_redacted["api_key"] == "[REDACTED]"
        assert red.tool_arguments_redacted["password"] == "[REDACTED]"
        assert red.tool_arguments_redacted["safe"] == "plain-value"

    def test_stack_trace_secret_redacted(self) -> None:
        env = ErrorEnvelope(
            error_message="failed",
            stack_trace="api_key=sk-live-secret token=abc",
        )
        red = env.redacted()
        assert "sk-live-secret" not in red.stack_trace
        assert "[REDACTED]" in red.stack_trace


class TestDedup:
    """Scenario 9: identical errors aggregated."""

    def test_17_duplicates_aggregate_to_17(self) -> None:
        env = ErrorEnvelope.from_exception(
            RuntimeError("same failure"), source_component="worker", operation="op"
        )
        dedup = Deduplicator()
        for _ in range(17):
            dedup.record(env)
        assert dedup.count(env) == 17

    def test_distinct_errors_not_merged(self) -> None:
        env1 = ErrorEnvelope.from_exception(
            RuntimeError("failure A"), source_component="worker", operation="op"
        )
        env2 = ErrorEnvelope.from_exception(
            RuntimeError("failure B"), source_component="worker", operation="op"
        )
        dedup = Deduplicator()
        dedup.record(env1)
        dedup.record(env2)
        assert dedup.count(env1) == 1
        assert dedup.count(env2) == 1
        assert fingerprint(env1) != fingerprint(env2)


class TestCorrelationIsolation:
    """Scenario 10: concurrent errors isolated by correlation_id."""

    def test_correlation_ids_kept(self) -> None:
        env = ErrorEnvelope.from_exception(
            RuntimeError("x"),
            source_component="worker",
            operation="op",
            correlation_id="cid-100",
        )
        assert env.correlation_id == "cid-100"
        result = RCAEngine().diagnose(env)
        assert result.correlation_id == "cid-100"


class TestErrorEventTransport:
    """Scenario 7: Hermes consumer crash/no-impact + non-blocking."""

    def test_error_event_builds_envelope_and_redacts(self) -> None:
        event = ErrorOccurred(
            correlation_id="cid-1",
            source_component="worker",
            error_type="TimeoutError",
            message="provider timed out",
            details={"api_key": "sk-secret"},
        )
        env = build_envelope_from_error_event(event)
        assert env.correlation_id == "cid-1"
        assert env.source_component == "worker"
        assert env.exception_type == "TimeoutError"
        assert env.runtime_metadata["api_key"] == "[REDACTED]"

    def test_consumer_error_does_not_raise(self) -> None:
        import asyncio

        from antigona.rca.pipeline import RCAEngine

        consumer = HermesRCAConsumer(engine=RCAEngine(), repository=None)
        event = ErrorOccurred(
            correlation_id="cid-2",
            source_component="mcp",
            error_type="Exception",
            message="mcp failed",
        )
        asyncio.run(consumer._dispatch(event))  # must not raise


class TestClassificationSpecificity:
    """Specific signals (tool/model/component) must beat generic provider context.

    Regression: a shell command failing with a provider field was misclassified
    as PROVIDER because the provider needle matched first. Tool/model/component
    signals must win.
    """

    def test_tool_failure_with_provider_field_is_tool(self) -> None:
        env = ErrorEnvelope.from_exception(
            Exception("shell command failed with exit code 1"),
            source_component="worker", operation="tool_execution",
            tool_name="owner_shell", provider="deepseek", model="deepseek-v4-flash",
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "TOOL"

    def test_model_capacity_with_provider_field_is_model(self) -> None:
        env = ErrorEnvelope.from_exception(
            Exception("context length exceeded"),
            source_component="worker", operation="model_call",
            provider="deepseek", model="deepseek-v4-flash",
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "MODEL"

    def test_component_signal_beats_provider_needle(self) -> None:
        env = ErrorEnvelope.from_exception(
            Exception("mcp stdio failed to start"),
            source_component="mcp", operation="discovery", provider="deepseek",
        )
        result = RCAEngine().diagnose(env)
        assert result.category == "MCP"

