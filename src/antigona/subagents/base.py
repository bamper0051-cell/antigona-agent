"""SubagentAdapter Protocol — contract for external coding agent delegation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class TaskType(StrEnum):
    """Type of task a subagent can execute."""

    PLANNING = "PLANNING"
    CODING = "CODING"
    REVIEW = "REVIEW"
    DEBUG = "DEBUG"
    TESTING = "TESTING"
    RESEARCH = "RESEARCH"


class ExecutionStatus(StrEnum):
    """Lifecycle status of a delegated execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class SubagentResult:
    """Result of a subagent execution."""

    execution_id: str
    status: ExecutionStatus
    output: str
    error: str | None = None
    exit_code: int = 0
    artifacts: tuple[str, ...] = ()


@runtime_checkable
class SubagentAdapter(Protocol):
    """Protocol for delegating tasks to an external coding agent (CLI)."""

    name: str

    async def execute(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> SubagentResult:
        """Dispatch *task* to the external agent CLI.

        Args:
            task: The task description or prompt to delegate.
            context: Optional context dict (workspace path, env vars, …).

        Returns:
            A SubagentResult describing the outcome.
        """
        ...

    async def get_status(self, execution_id: str) -> SubagentResult:
        """Poll the status of a previously submitted execution.

        Args:
            execution_id: Identifier returned by execute().

        Returns:
            Current SubagentResult (status may be RUNNING if not done).
        """
        ...

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution.

        Args:
            execution_id: Identifier returned by execute().
        """
        ...
