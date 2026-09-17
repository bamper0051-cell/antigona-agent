"""Base Executor interface — abstract base for action execution.

An Executor takes a plan step and executes it, returning the result.
Concrete implementations may run shell commands, write files,
make HTTP requests, or delegate to external tools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Executor(ABC):
    """Abstract base for executing a single action step.

    Each action step from the planner is dispatched to an Executor
    that knows how to handle that action type.
    """

    @abstractmethod
    async def execute(self, step: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a single action step.

        Args:
            step: Action step dict with 'action', 'params', 'description'.
            context: Optional execution context.

        Returns:
            Result dict with keys:
                - success: bool
                - output: Any — the result of execution.
                - error: str | None — error message if failed.
        """
        ...


class PassthroughExecutor(Executor):
    """Minimal executor that logs and returns step info.

    Useful as a no-op default or for testing.
    """

    async def execute(self, step: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "success": True,
            "output": {
                "action": step.get("action"),
                "params": step.get("params"),
                "message": "PassthroughExecutor: no-op execution",
            },
            "error": None,
        }
