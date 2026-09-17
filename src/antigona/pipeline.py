from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Artifact, FlowStep, TaskFlow


@dataclass(frozen=True)
class Plan:
    steps: tuple[str, ...]


class Planner(Protocol):
    def plan(self, task: TaskFlow) -> Plan: ...


class Executor(Protocol):
    def execute(self, task: TaskFlow, step: FlowStep) -> Artifact | None: ...


class CompletionVerifier(Protocol):
    def request_verification(self, task_id: str, correlation_id: str) -> str: ...


class DeterministicPlanner:
    """Replaceable P0 planner; orchestration depends only on Planner."""
    def plan(self, task: TaskFlow) -> Plan:
        if task.steps:
            steps_tuple = tuple(
                f"{s.tool_name or task.tool_name}:{task.target_path}"
                for s in sorted(task.steps, key=lambda x: x.index)
            ) + (f"verify:{task.target_path}",)
            return Plan(steps_tuple)
        return Plan((f"{task.tool_name}:{task.target_path}", f"verify:{task.target_path}"))
