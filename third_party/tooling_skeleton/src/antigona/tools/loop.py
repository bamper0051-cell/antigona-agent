from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .capabilities import CapabilitySnapshot, build_capability_snapshot
from .contracts import ToolCall, ToolCallRecord, ToolContext, ToolResult
from .executor import ToolExecutor


@dataclass
class AgentState:
    goal: str
    completion_criteria: tuple[str, ...]
    history: list[ToolCallRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolAction:
    call: ToolCall


@dataclass(frozen=True)
class FinishAction:
    answer: str


AgentAction = ToolAction | FinishAction


class Planner(Protocol):
    async def next_action(
        self,
        state: AgentState,
        capabilities: CapabilitySnapshot,
    ) -> AgentAction: ...


class AgentRunner:
    def __init__(self, executor: ToolExecutor, planner: Planner, *, max_iterations: int = 12) -> None:
        self.executor = executor
        self.planner = planner
        self.max_iterations = max_iterations

    async def run(self, state: AgentState, context: ToolContext) -> str:
        for _ in range(self.max_iterations):
            snapshot = build_capability_snapshot(self.executor.registry, context)
            action = await self.planner.next_action(state, snapshot)
            if isinstance(action, FinishAction):
                return action.answer

            result = await self.executor.execute(action.call, context, state.history)
            state.history.append(ToolCallRecord(action.call, result))
            state.notes.append(self._interpret(result))

        return "Операция остановлена: исчерпан лимит итераций без подтверждённого завершения."

    @staticmethod
    def _interpret(result: ToolResult) -> str:
        return f"{result.tool_name}: {result.status} — {result.summary}"
