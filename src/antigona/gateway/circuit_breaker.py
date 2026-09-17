"""Circuit breaker for Gateway HTTP client.

Provides degradation-aware gateway access with:
- State machine (CLOSED → OPEN → HALF_OPEN → CLOSED)
- In-memory deferred task queue
- Recovery detection and notification
"""

from __future__ import annotations

import time
from dataclasses import dataclass

FAILURE_THRESHOLD = 3
RECOVERY_TIMEOUT = 15  # seconds before probing recovery


@dataclass
class DeferredTask:
    """A task request queued while Gateway is unavailable."""

    goal: str
    path: str
    content: str
    tool_name: str
    command: list[str]
    idempotency_key: str = ""


class GatewayCircuitBreaker:
    """Circuit breaker for Gateway HTTP client.

    States:
        CLOSED  — normal operation, all requests pass through.
        OPEN    — degraded mode, gateway considered unavailable.
        HALF_OPEN — recovery probe in progress; single request allowed.

    Transitions:
        CLOSED → OPEN  — after FAILURE_THRESHOLD consecutive failures.
        OPEN → HALF_OPEN — after RECOVERY_TIMEOUT seconds have elapsed.
        HALF_OPEN → CLOSED — single successful probe / request.
        HALF_OPEN → OPEN   — single failed probe / request.
    """

    def __init__(self) -> None:
        self.state: str = "CLOSED"
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.deferred_tasks: list[DeferredTask] = []
        self._recovery_notified: bool = False
        self._degraded_notified: bool = False
        self._probe_in_flight: bool = False

    # ── Public API ───────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if gateway is currently considered available.

        Returns True for CLOSED state or during HALF_OPEN probe.
        For OPEN state, checks if enough time has passed for recovery probe.
        """
        if self.state == "CLOSED":
            return True
        if self.state == "HALF_OPEN":
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

        # OPEN — allow a probe after recovery timeout
        if time.monotonic() - self.last_failure_time >= RECOVERY_TIMEOUT:
            self.state = "HALF_OPEN"
            self._probe_in_flight = True
            return True

        return False

    def record_success(self) -> None:
        """Record a successful gateway call. Resets failure count."""
        self._probe_in_flight = False
        prev = self.state
        if prev == "HALF_OPEN":
            self._recovery_notified = True  # signal for recovery message
        self.state = "CLOSED"
        self.failure_count = 0
        self._degraded_notified = False

    def record_failure(self) -> None:
        """Record a gateway failure. May trigger OPEN transition."""
        self._probe_in_flight = False
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= FAILURE_THRESHOLD:
            self.state = "OPEN"

    def consume_recovery_flag(self) -> bool:
        """Check and consume the 'just recovered' notification flag.

        Returns True exactly once after a HALF_OPEN → CLOSED transition.
        """
        if self._recovery_notified:
            self._recovery_notified = False
            return True
        return False

    def is_closed(self) -> bool:
        """Check if circuit breaker is in CLOSED state."""
        return self.state == "CLOSED"

    def is_open(self) -> bool:
        """Check if circuit breaker is in OPEN state."""
        return self.state == "OPEN"

    def enqueue_task(self, task: DeferredTask) -> None:
        """Add a task to the deferred queue for later execution."""
        self.deferred_tasks.append(task)
        if len(self.deferred_tasks) > 50:
            self.deferred_tasks = self.deferred_tasks[-50:]

    def flush_tasks(self) -> list[DeferredTask]:
        """Extract all deferred tasks for sending.

        Returns a copy of the queue and clears it.
        """
        tasks = list(self.deferred_tasks)
        self.deferred_tasks.clear()
        return tasks

    @property
    def queued_task_count(self) -> int:
        """Number of tasks waiting in the deferred queue."""
        return len(self.deferred_tasks)
