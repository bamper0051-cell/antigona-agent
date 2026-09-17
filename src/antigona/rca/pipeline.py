"""Hermes RCA worker — fixed diagnostic pipeline (spec section 8).

Stages:
  A. Classification  (APPLICATION/INFRASTRUCTURE/NETWORK/DATABASE/REDIS/
                      PROVIDER/MODEL/TOOL/POLICY/AUTH/MCP/DURABLE_STATE/
                      VERIFIER/CONFIGURATION/DEPENDENCY/UNKNOWN)
  B. Evidence collection (scoped reads only — never full-system scans)
  C. Root Cause Analysis (symptom vs root cause)
  D. Confidence (LOW/MEDIUM/HIGH/CONFIRMED — CONFIRMED needs direct evidence)
  E. Remediation suggestion (proposed as data only, never executed)

This worker is read-only and self-contained so Antigona keeps working when no
external Hermes is reachable; ``available`` is reported on every result.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

from antigona.contracts import Evidence
from antigona.rca.envelope import ErrorEnvelope
from antigona.rca.result import RCAConfidence, RCAResult

# ── Stage A classification rules ─────────────────────────────────────────────


#: source_component -> category (strongest structural signal, wins over needles).
_COMPONENT_CATEGORY: dict[str, str] = {
    "mcp": "MCP",
    "policy": "POLICY",
    "verifier": "VERIFIER",
    "durable": "DURABLE_STATE",
    "provider": "PROVIDER",
    "database": "DATABASE",
    "redis": "REDIS",
    "config": "CONFIGURATION",
    "auth": "AUTH",
}

#: Tool-name prefixes that pin the error to TOOL regardless of provider context.
_TOOL_COMPONENT = "tool"

_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("AUTH", ["authentication", "unauthorized", "403", "401", "token_expired", "invalid_credentials", "bearer"]),
    ("POLICY", ["policy", "denied", "deny", "not allowed", "requires_approval", "approval", "permission"]),
    ("REDIS", ["redis", "connection refused", "maxmemory", "command denied"]),
    ("DATABASE", ["database", "sqlite", "postgres", "sqlalchemy", "integrityerror", "operationalerror", "db ", "transaction"]),
    ("NETWORK", ["network", "timeout", "connection reset", "connection refused", "socket", "dns", "connect ", "ssl", "tls"]),
    ("PROVIDER", ["provider", "openrouter", "deepseek", "anthropic", "openai", "api_key", "rate limit", "quota", "429", "provider_error"]),
    ("MODEL", ["model", "context length", "token limit", "max_tokens", "model_not_found", "completion"]),
    ("TOOL", ["tool", "tool_call", "executor", "command failed", "exit code", "shell"]),
    ("MCP", ["mcp", "stdio", "mcpserver"]),
    ("DURABLE_STATE", ["durable", "flow_state", "operation_store", "state_machine", "taskflow", "lease"]),
    ("VERIFIER", ["verifier", "verdict", "verification"]),
    ("CONFIGURATION", ["config", "settings", "environment variable", "misconfigured", "invalid setting"]),
    ("DEPENDENCY", ["importerror", "modulenotfounderror", "no module", "dependency", "package not found"]),
    ("INFRASTRUCTURE", ["worker", "gateway", "crash", "restart", "oom", "memory", "disk", "cpu"]),
]


def classify(envelope: ErrorEnvelope) -> str:
    """Stage A — classify the error into one canonical category.

    Field-aware precedence so specific signals win over generic provider
    context:
      1. Explicit source_component (mcp/policy/verifier/durable/...) wins.
      2. A tool that failed pins TOOL (a shell command failing because the
         caller also mentions a provider is still a TOOL error, not PROVIDER).
      3. Otherwise the first matching needle rule (specific first).
    """
    comp = (envelope.source_component or "").lower()
    tool = (envelope.tool_name or "").lower()

    if comp:
        if comp in _COMPONENT_CATEGORY:
            return _COMPONENT_CATEGORY[comp]
        # A non-mapped component that names a tool is still a TOOL failure.
        if tool and comp != "worker":
            return "TOOL"
        if tool and ("shell" in comp or "executor" in comp or "tool" in comp):
            return "TOOL"

    # Explicit tool signal (tool_name set) pins TOOL before provider needles.
    if tool and not (envelope.provider and _is_pure_provider_error(envelope)):
        return "TOOL"

    haystack = " ".join(
        filter(
            None,
            [
                envelope.source_component,
                envelope.operation,
                envelope.exception_type,
                envelope.error_message,
                tool,
                envelope.provider or "",
            ],
        )
    ).lower()
    # Model-capacity signals must win over generic provider context (a provider
    # field is not evidence of a provider error when the model itself is the issue).
    if any(k in haystack for k in ("context length", "token limit", "max_tokens", "model_not_found", "model not found", "completion")):
        return "MODEL"
    for category, needles in _CATEGORY_RULES:
        for needle in needles:
            if needle in haystack:
                return category
    return "UNKNOWN"


def _is_pure_provider_error(envelope: ErrorEnvelope) -> bool:
    """True only when the provider/model call itself is the failure (no tool)."""
    msg = f"{envelope.exception_type} {envelope.error_message}".lower()
    return any(k in msg for k in ("timed out", "rate limit", "quota", "context length", "max_tokens", "429", "401", "403", "provider"))


# ── Stage D confidence ───────────────────────────────────────────────────────

def _assess_confidence(category: str, envelope: ErrorEnvelope) -> RCAConfidence:
    """Confidence from category + presence of direct evidence."""
    # CONFIRMED requires direct evidence — here we require a concrete exception
    # type and a non-empty stack trace attributable to the category.
    if envelope.exception_type and envelope.stack_trace and category != "UNKNOWN":
        return RCAConfidence.HIGH
    if category != "UNKNOWN":
        return RCAConfidence.MEDIUM
    return RCAConfidence.LOW


# ── Stage C symptom-vs-root-cause heuristics ────────────────────────────────

_TIMEOUT_RE = re.compile(r"timeout|timed out|stall|stalled", re.I)
_LEASE_RE = re.compile(r"lease|expiration|expired", re.I)
_PROVIDER_RE = re.compile(r"provider|openrouter|deepseek|anthropic|openai", re.I)


def _root_cause(envelope: ErrorEnvelope) -> tuple[str, str]:
    """Stage C — return (root_cause, user_impact) separating symptom from cause.

    Heuristic: if the envelope mentions a provider timeout while a worker lease
    expired, report the *root cause* (provider stall) rather than the symptom
    (worker timeout), matching the spec example.
    """
    msg = f"{envelope.error_message} {envelope.source_component}".lower()
    is_timeout = bool(_TIMEOUT_RE.search(msg))
    has_provider = bool(_PROVIDER_RE.search(msg))
    has_lease = bool(_LEASE_RE.search(msg))

    if is_timeout and has_provider and has_lease:
        return (
            "Provider request stalled (network/model latency), causing worker lease "
            "expiration and loss of commit authority.",
            "Task failure: worker lost its lease and could not commit the step.",
        )
    if is_timeout and has_provider:
        return (
            "Provider request timed out; the model/provider layer did not return "
            "within the configured window.",
            "Task delayed/failed at the model/provider call.",
        )
    if has_provider:
        return (
            "Provider-layer error (auth, quota, connectivity or model availability).",
            "Task blocked at the model/provider step.",
        )
    return (
        f"Failure in {envelope.source_component or 'unknown'} during {envelope.operation or 'operation'}.",
        "The affected operation did not complete.",
    )


def _remediation(category: str, envelope: ErrorEnvelope) -> list[str]:
    """Stage E — propose remediation as *data only* (never executed here)."""
    actions: list[str] = []
    if category == "PROVIDER":
        actions.append("Retry the provider request with backoff (transient network/rate-limit).")
        actions.append("Verify provider API key/quotas; consider a different provider profile.")
    elif category == "POLICY":
        actions.append("Request owner approval for the denied action via the approval pipeline.")
    elif category == "DATABASE":
        actions.append("Check DB connection/transaction state; verify schema/migrations.")
    elif category == "REDIS":
        actions.append("Check Redis connectivity and memory/eviction policy.")
    elif category == "NETWORK":
        actions.append("Verify outbound connectivity and DNS; retry with backoff.")
    elif category == "MODEL":
        actions.append("Reduce context/token usage or switch model/profile.")
    elif category == "TOOL":
        actions.append("Inspect the failing tool's exit code/arguments; re-run with corrected input.")
    elif category == "MCP":
        actions.append("Check the MCP server registration and stdio/HTTP routing.")
    elif category == "AUTH":
        actions.append("Re-authenticate/refresh credentials; verify token validity.")
    elif category == "DURABLE_STATE":
        actions.append("Reconcile durable flow/operation state; verify lease ownership.")
    elif category == "VERIFIER":
        actions.append("Review verifier criteria and re-run verification.")
    elif category == "CONFIGURATION":
        actions.append("Review runtime configuration/environment for the affected component.")
    elif category == "DEPENDENCY":
        actions.append("Install/resolve the missing dependency in the environment.")
    else:
        actions.append("Inspect component logs and re-run the operation to confirm.")
    actions.append("All remediation requires separate Antigona owner approval — Hermes does not apply changes.")
    return actions


# ── Evidence (Stage B) ───────────────────────────────────────────────────────

def _evidence(envelope: ErrorEnvelope) -> list[Evidence]:
    items: list[Evidence] = []
    if envelope.exception_type:
        items.append(Evidence("exception_type", envelope.exception_type))
    if envelope.error_message:
        items.append(Evidence("error_message", envelope.error_message))
    if envelope.source_component:
        items.append(Evidence("source_component", envelope.source_component))
    if envelope.tool_name:
        items.append(Evidence("tool", envelope.tool_name))
    if envelope.provider:
        items.append(Evidence("provider", envelope.provider))
    if envelope.stack_trace:
        # only a short sanitized head as evidence — never the full trace
        head = "\n".join(envelope.stack_trace.splitlines()[:6])
        items.append(Evidence("stack_head", head))
    if envelope.policy_decision:
        items.append(Evidence("policy", str(envelope.policy_decision.get("category", "denied"))))
    return items


def analyze(envelope: ErrorEnvelope, *, available: bool = True) -> RCAResult:
    """Run the full Hermes RCA diagnostic pipeline on an envelope.

    Returns an RCAResult. Never mutates anything; purely read-only diagnostic.
    """
    start = time.monotonic()
    category = classify(envelope)
    confidence = _assess_confidence(category, envelope)
    root_cause, impact = _root_cause(envelope)
    actions = _remediation(category, envelope)
    affected = [envelope.source_component] if envelope.source_component else []
    if envelope.provider:
        affected.append(f"provider:{envelope.provider}")

    duration_ms = round((time.monotonic() - start) * 1000, 3)
    return RCAResult(
        rca_id=f"rca_{uuid.uuid4().hex[:12]}",
        error_id=envelope.error_id,
        correlation_id=envelope.correlation_id,
        category=category,
        summary=f"Detected {category.lower()} failure in {envelope.source_component or 'component'}.",
        root_cause=root_cause,
        evidence=_evidence(envelope),
        confidence=confidence,
        affected_components=affected,
        user_impact=impact,
        recommended_actions=actions,
        safe_to_auto_fix=False,
        requires_owner_approval=True,
        created_at=envelope.timestamp,
        analysis_duration_ms=duration_ms,
        hermes_available=available,
    )


@dataclass
class RCAEngine:
    """Entry point used by the event transport and CLI commands."""

    available: bool = True

    def diagnose(self, envelope: ErrorEnvelope) -> RCAResult:
        return analyze(envelope, available=self.available)
