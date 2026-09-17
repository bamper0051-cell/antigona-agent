"""Autonomous Goal Orchestration (M2) — capacity-aware service router.

``ServiceRouter`` picks an executor service for a task: capability match +
availability + capacity + independence + failure history.

This is the capacity-aware routing layer (E2E-7) and the failover ladder
(E2E-4/5). Routing is read-only against the durable ``OrchestrationStore``;
outcomes are recorded back through ``record_success`` / ``record_failure``
so the next route decision sees updated failure history.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from .models import ServiceHealthState
from .state import Capacity, FailureClass, ServiceState
from .store import OrchestrationStore

#: Fallback order for implementation roles (grok = audit-only, never
#: implementation; hermes = planner/orchestrator, NOT an execution service —
#: an auto-success Hermes stub would mint false DONE evidence).
#: Keep ROLE_TO_SERVICES as the single source of truth.

#: Capability map: role -> candidate services in preference order.
ROLE_TO_SERVICES: dict[str, list[str]] = {
    "implementer": ["claude", "codex", "agy"],
    "reviewer": ["codex", "claude", "agy"],
    "verifier": ["agy", "codex", "claude"],
    "auditor": ["grok"],
}


class ServiceCapability(StrEnum):
    ANALYZE_READ_ONLY = "ANALYZE_READ_ONLY"
    WRITE_WORKSPACE = "WRITE_WORKSPACE"
    RUN_TESTS = "RUN_TESTS"
    VERIFY_READ_ONLY = "VERIFY_READ_ONLY"


_FULL_CAPABILITIES = frozenset(ServiceCapability)
SERVICE_CAPABILITIES: dict[str, frozenset[ServiceCapability]] = {
    "claude": _FULL_CAPABILITIES,
    "codex": _FULL_CAPABILITIES,
    "agy": _FULL_CAPABILITIES,
    "grok": frozenset({
        ServiceCapability.ANALYZE_READ_ONLY,
        ServiceCapability.RUN_TESTS,
        ServiceCapability.VERIFY_READ_ONLY,
    }),
}


def required_capabilities(task_payload: dict[str, Any]) -> frozenset[ServiceCapability]:
    stage = str(task_payload.get("stage") or "")
    if stage == "analysis":
        return frozenset({ServiceCapability.ANALYZE_READ_ONLY})
    if stage == "implementation" and bool(task_payload.get("mutation_required")):
        return frozenset({
            ServiceCapability.ANALYZE_READ_ONLY,
            ServiceCapability.WRITE_WORKSPACE,
            ServiceCapability.RUN_TESTS,
        })
    if stage == "implementation":
        return frozenset({ServiceCapability.ANALYZE_READ_ONLY})
    if stage == "verification":
        return frozenset({
            ServiceCapability.RUN_TESTS,
            ServiceCapability.VERIFY_READ_ONLY,
        })
    return frozenset()


def service_has_capabilities(service_id: str, required: frozenset[ServiceCapability]) -> bool:
    return required <= SERVICE_CAPABILITIES.get(service_id, frozenset())

#: Health states that disqualify a service immediately (no fallback tier).
_SKIP_STATES = frozenset(
    {
        ServiceState.BUSY,
        ServiceState.RATE_LIMITED,
        ServiceState.QUOTA_LOW,
        ServiceState.UNAVAILABLE,
    }
)

#: After this much time since the last failure, a RATE_LIMITED/UNAVAILABLE
#: service is allowed to re-enter the pool (probing tier) instead of being
#: skipped forever — a rate limit or outage is usually transient.
_FAILURE_COOLDOWN_SECONDS = 300

#: Failure classes that map to a non-DEGRADED health state.
_RATE_LIMITED_CLASSES = frozenset(
    {FailureClass.RATE_LIMIT.value, FailureClass.QUOTA_EXHAUSTED.value}
)
_UNAVAILABLE_CLASSES = frozenset(
    {FailureClass.AUTH_FAILURE.value, FailureClass.PROVIDER_OUTAGE.value}
)


def _service_state(health: ServiceHealthState | None) -> ServiceState:
    """Health state as enum; a missing/unknown row probes as UNKNOWN."""
    if health is None or not health.state:
        return ServiceState.UNKNOWN
    try:
        return ServiceState(health.state)
    except ValueError:
        return ServiceState.UNKNOWN


def _capacity(health: ServiceHealthState | None) -> Capacity:
    if health is None or not health.capacity:
        return Capacity.UNKNOWN
    try:
        return Capacity(health.capacity)
    except ValueError:
        return Capacity.UNKNOWN


def _capacity_score(cap: Capacity, size: str) -> int:
    """0 = preferred capacity for the task size, 1 = everything else.

    Preference is soft: routing still succeeds when no preferred-capacity
    service exists (E2E-7 saves high capacity for large tasks, it does not
    hard-fail medium/small tasks).
    """
    if size == "large":
        return 0 if cap == Capacity.HIGH else 1
    if size == "small":
        return 0 if cap in (Capacity.LOW, Capacity.MEDIUM) else 1
    return 0  # medium (or unknown size): no capacity preference


def ladder_fallback(primary: str | None, avoid: set[str]) -> str | None:
    """If ``primary`` is None, walk the implementer ladder skipping ``avoid``."""
    if primary is not None:
        return primary
    return next(
        (sid for sid in ROLE_TO_SERVICES["implementer"] if sid not in avoid),
        None,
    )


class ServiceRouter:
    def __init__(self, orch: OrchestrationStore) -> None:
        self._orch = orch

    def route(
        self,
        task_payload: dict[str, Any],
        role: str = "implementer",
        avoid: set[str] | None = None,
        size: str = "medium",
    ) -> str | None:
        """Return a service_id or None if none available."""
        from datetime import UTC, datetime

        avoided = set() if avoid is None else avoid
        required = required_capabilities(task_payload)
        # Unknown roles must never widen to the audit-only ladder (grok must
        # not become an implementer); fall back to implementer candidates.
        candidates = [
            sid
            for sid in ROLE_TO_SERVICES.get(role, ROLE_TO_SERVICES["implementer"])
            if sid not in avoided and service_has_capabilities(sid, required)
        ]
        hint = task_payload.get("service_hint")
        size_norm = (size or "medium").lower()
        now = datetime.now(UTC)

        eligible: list[tuple[tuple[int, int, int, int], str]] = []
        for sid in candidates:
            health = self._orch.get_health(sid)
            state = _service_state(health)
            if state in _SKIP_STATES:
                # Cooldown: a rate-limited/unavailable service may re-enter the
                # pool after _FAILURE_COOLDOWN_SECONDS (transient failures).
                if health and health.last_failure_at:
                    try:
                        last_fail = health.last_failure_at
                        if last_fail.tzinfo is None:
                            last_fail = last_fail.replace(tzinfo=UTC)
                        if (now - last_fail).total_seconds() >= _FAILURE_COOLDOWN_SECONDS:
                            state = ServiceState.UNKNOWN  # re-probe tier
                        else:
                            continue
                    except (TypeError, ValueError):
                        continue
                else:
                    continue
            # AVAILABLE/UNKNOWN are first-class; DEGRADED only if nothing better.
            tier = 0 if state != ServiceState.DEGRADED else 1
            hint_rank = 0 if hint == sid else 1
            cap_score = _capacity_score(_capacity(health), size_norm)
            failures = health.failure_count if health and health.failure_count else 0
            eligible.append(((tier, hint_rank, cap_score, failures), sid))

        # Stable sort: candidates keep ladder order on ties.
        eligible.sort(key=lambda item: item[0])
        return eligible[0][1] if eligible else None

    def record_success(self, service_id: str) -> None:
        """Mark AVAILABLE, keep capacity, reset failure history."""
        self._orch.reset_failures(service_id)

    def record_failure(self, service_id: str, failure_class: FailureClass | str) -> None:
        """Mark the service per failure class; increments failure_count."""
        cls = failure_class.value if isinstance(failure_class, FailureClass) else failure_class
        cls = cls.upper()
        if cls in _RATE_LIMITED_CLASSES:
            state = ServiceState.RATE_LIMITED
        elif cls in _UNAVAILABLE_CLASSES:
            state = ServiceState.UNAVAILABLE
        else:  # TIMEOUT, PROCESS_CRASH, INVALID_OUTPUT, TASK_FAILURE, ...
            state = ServiceState.DEGRADED
        self._orch.set_health(service_id, state, failure_class=cls)

    def choose_for_role(
        self,
        role: str,
        avoid: set[str] | None = None,
        size: str = "medium",
    ) -> str | None:
        """Convenience: route({}, role=role, avoid=avoid, size=size)."""
        return self.route({}, role=role, avoid=avoid, size=size)
