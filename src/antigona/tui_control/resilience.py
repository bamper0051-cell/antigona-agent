"""Д35 — Resilience pass: retry budgets, circuit breakers, queues.

This module provides the resilience primitives (CircuitBreaker, RetryBudget,
ResilientQueue) and a TUI panel that renders their state.  No business logic
in the panel — it only reads state from the EventBus.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Label, Static

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "ResiliencePanel",
    "ResilientQueue",
    "RetryBudget",
]

# ── Resilience primitives ─────────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class RetryBudget:
    """Track retry attempts and decide whether retry is allowed.

    A budget has a max_retries and a window_seconds.  Retries older than
    the window expire and don't count against the limit.
    """

    max_retries: int = 3
    window_seconds: float = 60.0
    _attempts: list[float] = field(default_factory=list)

    def allow_retry(self) -> bool:
        """Check whether another retry is allowed within the budget."""
        now = time.monotonic()
        # Prune expired entries
        self._attempts = [t for t in self._attempts if now - t < self.window_seconds]
        return len(self._attempts) < self.max_retries

    def record_attempt(self) -> None:
        """Record a retry attempt."""
        self._attempts.append(time.monotonic())

    def reset(self) -> None:
        """Reset all attempts."""
        self._attempts.clear()

    @property
    def remaining(self) -> int:
        now = time.monotonic()
        active = sum(1 for t in self._attempts if now - t < self.window_seconds)
        return max(0, self.max_retries - active)

    @property
    def total_attempts(self) -> int:
        return len(self._attempts)


class CircuitBreaker:
    """Circuit breaker with configurable thresholds.

    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (probing).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        name: str = "",
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._success_count = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
        elif self._state == CircuitState.CLOSED:
            self._success_count += 1

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        return self.state != CircuitState.OPEN

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._success_count = 0

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def state_label(self) -> str:
        return self.state.value


class ResilientQueue:
    """A simple queue with depth tracking and timeout support."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._items: list[dict[str, Any]] = []
        self._processed = 0
        self._failed = 0

    def enqueue(self, item: dict[str, Any]) -> None:
        self._items.append(item)

    def dequeue(self) -> dict[str, Any] | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def mark_processed(self) -> None:
        self._processed += 1

    def mark_failed(self) -> None:
        self._failed += 1

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def total_processed(self) -> int:
        return self._processed

    @property
    def total_failed(self) -> int:
        return self._failed


# ── Resilience Panel ───────────────────────────────────────────────────────────


class ResiliencePanel(Static):
    """Resilience dashboard — circuit breakers, retry budgets, queue depth.

    Pure-presentation: renders state received from EventBus.
    """

    circuits_open: reactive[int] = reactive(0)
    queue_depth: reactive[int] = reactive(0)
    retries_used: reactive[int] = reactive(0)

    CIRCUIT_COLUMNS = ("Circuit", "State", "Failures", "Successes", "Threshold")
    QUEUE_COLUMNS = ("Queue", "Depth", "Processed", "Failed")
    RETRY_COLUMNS = ("Budget", "Remaining", "Max", "Window")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._breakers: dict[str, CircuitBreaker] = {}
        self._queues: dict[str, ResilientQueue] = {}
        self._budgets: dict[str, RetryBudget] = {}

    def on_mount(self) -> None:
        self.query_one("#circuit_table", DataTable).add_columns(*self.CIRCUIT_COLUMNS)
        self.query_one("#queue_table", DataTable).add_columns(*self.QUEUE_COLUMNS)
        self.query_one("#retry_table", DataTable).add_columns(*self.RETRY_COLUMNS)

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("Resilience Dashboard", classes="panel_title"),
            Static(id="resilience_stats", classes="panel_stats"),
        )
        yield Label("Circuit Breakers")
        yield DataTable(id="circuit_table", cursor_type="row")
        yield Label("Queues")
        yield DataTable(id="queue_table", cursor_type="row")
        yield Label("Retry Budgets")
        yield DataTable(id="retry_table", cursor_type="row")

    def register_breaker(self, name: str, breaker: CircuitBreaker) -> None:
        self._breakers[name] = breaker
        self._rebuild()

    def register_queue(self, name: str, queue: ResilientQueue) -> None:
        self._queues[name] = queue
        self._rebuild()

    def register_budget(self, name: str, budget: RetryBudget) -> None:
        self._budgets[name] = budget
        self._rebuild()

    def _rebuild(self) -> None:
        self._rebuild_circuits()
        self._rebuild_queues()
        self._rebuild_retries()
        self._update_stats()

    def _rebuild_circuits(self) -> None:
        try:
            table = self.query_one("#circuit_table", DataTable)
        except Exception:
            return
        table.clear()
        for name, cb in self._breakers.items():
            sts = cb.state_label
            table.add_row(
                name,
                Text(sts, style="green" if sts == "closed" else "red" if sts == "open" else "yellow"),
                str(cb.failure_count),
                str(getattr(cb, "_success_count", 0)),
                str(cb.failure_threshold),
            )

    def _rebuild_queues(self) -> None:
        try:
            table = self.query_one("#queue_table", DataTable)
        except Exception:
            return
        table.clear()
        for name, q in self._queues.items():
            table.add_row(name, str(q.depth), str(q.total_processed), str(q.total_failed))

    def _rebuild_retries(self) -> None:
        try:
            table = self.query_one("#retry_table", DataTable)
        except Exception:
            return
        table.clear()
        for name, b in self._budgets.items():
            table.add_row(name, str(b.remaining), str(b.max_retries), f"{b.window_seconds:.0f}s")

    def _update_stats(self) -> None:
        self.circuits_open = sum(
            1 for cb in self._breakers.values() if cb.state != CircuitState.CLOSED
        )
        self.queue_depth = sum(q.depth for q in self._queues.values())
        self.retries_used = sum(b.total_attempts for b in self._budgets.values())
        try:
            stats = self.query_one("#resilience_stats", Static)
            parts = [
                f"Open circuits: {self.circuits_open}",
                f"Queue depth: {self.queue_depth}",
                f"Retry attempts: {self.retries_used}",
            ]
            stats.update("  |  ".join(parts))
        except Exception:
            pass
