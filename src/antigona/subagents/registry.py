"""SubagentRegistry — selects the right adapter by task type."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from antigona.subagents.base import SubagentAdapter, TaskType

LOGGER = logging.getLogger("antigona.subagents.registry")


class AdapterNotFoundError(LookupError):
    """Raised when no adapter is registered for a given task type."""


class SubagentRegistry:
    """Manages available subagent adapters and selects by task type.

    Default routing:
        PLANNING → claude   (strong reasoning, planning)
        CODING   → codex    (fast code generation)
        REVIEW   → claude   (strong review capabilities)
        DEBUG    → claude   (deep reasoning)
        TESTING  → codex    (fast test scaffolding)
        RESEARCH → claude   (broad knowledge)

    Falls back: if primary adapter is unavailable → try secondary.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, SubagentAdapter] = {}

    def register(self, name: str, adapter: SubagentAdapter) -> None:
        """Register a named adapter."""
        self._adapters[name] = adapter

    def get(self, name: str) -> SubagentAdapter:
        """Look up an adapter by name."""
        adapter = self._adapters.get(name)
        if adapter is None:
            raise AdapterNotFoundError(f"No adapter registered under {name!r}")
        return adapter

    def select(self, task_type: TaskType) -> list[SubagentAdapter]:
        """Return adapters suitable for *task_type*, in preference order.

        Returns a list of candidate adapters so callers can implement
        fallback: try the first; if it fails → try the next.
        """
        from antigona.subagents.base import TaskType

        routing: dict[TaskType, tuple[str, ...]] = {
            TaskType.PLANNING: ("claude", "codex"),
            TaskType.CODING: ("codex", "claude"),
            TaskType.REVIEW: ("claude",),
            TaskType.DEBUG: ("claude", "codex"),
            TaskType.TESTING: ("codex", "claude"),
            TaskType.RESEARCH: ("claude",),
        }
        preferred = routing.get(task_type)
        if not preferred:
            raise AdapterNotFoundError(f"No routing for task type {task_type!r}")
        candidates: list[SubagentAdapter] = []
        for name in preferred:
            adapter = self._adapters.get(name)
            if adapter is not None:
                candidates.append(adapter)
        if not candidates:
            raise AdapterNotFoundError(
                f"No registered adapter for task type {task_type!r} "
                f"(wanted {list(preferred)})"
            )
        return candidates

    @property
    def available(self) -> frozenset[str]:
        """Names of all registered adapters."""
        return frozenset(self._adapters)

    def __repr__(self) -> str:
        return f"<SubagentRegistry adapters={set(self._adapters)}>"
