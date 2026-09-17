"""Worker integration — route read-only tasks to TurnWorker.

Provides a drop-in integration point that routes read-only file tasks
(``file_read``, ``file_check``) through the :class:`TurnWorker` (which runs
the full model → tool → result → model cycle with auto-repair) while
leaving write/shell tasks on the existing :class:`Orchestrator` path.

Usage in the worker main loop::

    executor = ReadOnlyTaskExecutor(base_url, api_key, model, workspace_path=str(paths.workspace_dir()))
    task = TaskRepository(session).get(job.task_id)

    if should_use_turn_worker(task):
        result = await executor.execute(task, correlation_id)
    else:
        result = Orchestrator(...).run(task, worker)

"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from antigona.turn_bridge.turn_engine_adapter import TurnBudget, TurnResult

from .worker_adapter import (
    READ_ONLY_TOOLS,
    TurnWorker,
    is_readonly_task,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class ReadOnlyExecutionResult:
    """Result of a read-only task execution.

    Attributes:
        success: Whether the task completed successfully.
        final_response: The model's final text response.
        error: Error message if the task failed.
        turns_used: Number of LLM turns used.
        tool_calls_made: Number of tool calls made.
        artifacts: Any artifacts produced.
    """

    success: bool = True
    final_response: str | None = None
    error: str | None = None
    turns_used: int = 0
    tool_calls_made: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)


def should_use_turn_worker(task_tool_name: str, task_command: tuple[str, ...] | None = None) -> bool:
    """Determine if a task should be routed to the TurnWorker.

    Read-only tasks (file_read, file_check, tool_read_file, etc.) are
    routed to the TurnWorker. Everything else (shell, write, etc.) goes
    to the existing Orchestrator path.

    Args:
        task_tool_name: The tool name from the task (e.g. ``file_read``).
        task_command: Optional command tuple (shell tasks have commands).

    Returns:
        True if the task should use TurnWorker.
    """
    if is_readonly_task(task_tool_name):
        return True
    # Also route tasks whose tool_name suggests read-only file operations
    # even if not in the exact set
    if task_tool_name and "read" in task_tool_name.lower() and "file" in task_tool_name.lower():
        return True
    return False


class ReadOnlyTaskExecutor:
    """Executes read-only tasks via TurnWorker.

    Thin wrapper that extracts the relevant fields from a TaskFlow-like
    dict and passes them to the TurnWorker.

    Args:
        base_url: OpenAI-compatible API base URL.
        api_key: API key.
        model: Model identifier.
        workspace_path: Absolute path to the workspace root.
        timeout_seconds: Provider request timeout.
        max_retries: Transient error retries for the provider.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        workspace_path: str | None = None,
        timeout_seconds: int = 120,
        max_retries: int = 2,
    ) -> None:
        self._worker = TurnWorker(
            base_url=base_url,
            api_key=api_key,
            model=model,
            workspace_path=workspace_path,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    @property
    def worker(self) -> TurnWorker:
        """The underlying TurnWorker instance."""
        return self._worker

    async def execute(
        self,
        flow_id: str,
        goal: str,
        messages: list[dict[str, Any]] | None = None,
        budget: TurnBudget | None = None,
    ) -> TurnResult:
        """Execute a read-only task.

        Args:
            flow_id: Flow/task identifier.
            goal: The goal description from the task.
            messages: Optional initial messages. Defaults to a single
                user message with the goal.
            budget: Optional budget overrides.

        Returns:
            A :class:`TurnResult` from the TurnEngine.
        """
        effective_messages = messages or [
            {"role": "user", "content": goal}
        ]

        return await self._worker.execute_task(
            flow_id=flow_id,
            goal=goal,
            messages=effective_messages,
            tools=READ_ONLY_TOOLS,
            budget=budget,
        )

    async def execute_from_task_dict(
        self,
        task_dict: dict[str, Any],
        budget: TurnBudget | None = None,
    ) -> TurnResult:
        """Execute a read-only task from a task dict (e.g. serialised TaskFlow).

        Args:
            task_dict: A dict representation of a TaskFlow.
            budget: Optional budget overrides.

        Returns:
            A :class:`TurnResult` from the TurnEngine.
        """
        flow_id = task_dict.get("id", "unknown")
        goal = task_dict.get("goal", "")
        messages = task_dict.get("messages")
        if not messages:
            # Build a user message from the goal and any additional context
            goal_text = goal
            if task_dict.get("content"):
                goal_text = f"{goal}\n\nContext content:\n{task_dict['content']}"
            messages = [{"role": "user", "content": goal_text}]

        return await self._worker.execute_task(
            flow_id=flow_id,
            goal=goal,
            messages=messages,
            tools=READ_ONLY_TOOLS,
            budget=budget,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._worker.close()


__all__ = [
    "ReadOnlyExecutionResult",
    "ReadOnlyTaskExecutor",
    "should_use_turn_worker",
]
