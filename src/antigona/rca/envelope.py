"""Hermes RCA — ErrorEnvelope contract and secret-redaction layer.

The envelope is the single canonical structured diagnostic object that any
Antigona subsystem publishes when it detects an error. It is redacted *before*
it is allowed to leave the process (published on the event bus / persisted),
so Hermes never sees raw credentials.

This module depends only on the existing ``observability.redact`` pipeline and
adds envelope-specific redaction of ``tool_arguments``, ``stack_trace`` and
free-form diagnostic strings.
"""

from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from antigona.observability import redact


@dataclass
class ErrorEnvelope:
    """Unified structured diagnostic object for a runtime error.

    Field set follows the mandatory Hermes RCA contract (spec section 5).
    ``tool_arguments_redacted`` and ``stack_trace`` are always passed through
    :func:`redact` at capture time so secrets never reach the bus/storage.
    """

    error_id: str = field(default_factory=lambda: f"err_{uuid4().hex[:12]}")
    correlation_id: str = ""
    task_id: str | None = None
    flow_id: str | None = None
    step_id: str | None = None
    session_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    source_component: str = ""
    source_module: str = ""
    operation: str = ""
    exception_type: str = ""
    error_message: str = ""
    stack_trace: str = ""
    severity: str = "MEDIUM"
    tool_name: str | None = None
    tool_arguments_redacted: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    policy_decision: dict[str, Any] | None = None
    approval_state: str = ""
    task_state: str = ""
    flow_state: str = ""
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    git_revision: str = ""
    environment: str = "production"
    diagnostic_hints: list[str] = field(default_factory=list)

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        source_component: str,
        operation: str,
        correlation_id: str = "",
        **overrides: Any,
    ) -> ErrorEnvelope:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        kwargs: dict[str, Any] = {
            "source_component": source_component,
            "operation": operation,
            "correlation_id": correlation_id,
            "exception_type": type(exc).__name__,
            "error_message": str(exc) or type(exc).__name__,
            "stack_trace": tb,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def redacted(self) -> ErrorEnvelope:
        """Redact every credential-bearing field (mandatory gate, spec sec 6)."""
        self.tool_arguments_redacted = redact(self.tool_arguments_redacted)  # type: ignore[assignment]
        self.stack_trace = redact(self.stack_trace)  # type: ignore[assignment]
        self.error_message = redact(self.error_message)  # type: ignore[assignment]
        self.runtime_metadata = redact(self.runtime_metadata)  # type: ignore[assignment]
        self.recent_events = redact(self.recent_events)  # type: ignore[assignment]
        if self.policy_decision is not None:
            self.policy_decision = redact(self.policy_decision)  # type: ignore[assignment]
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_redaction(obj: object) -> object:
    """Public re-export of the shared redaction pipeline (convenience)."""
    return redact(obj)
