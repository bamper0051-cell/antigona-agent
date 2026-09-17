from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running directly from a fresh checkout before `pip install -e .`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from antigona.tools import ToolCall, ToolContext, ToolExecutor, build_default_registry
from antigona.tools.loop import AgentRunner, AgentState, FinishAction, ToolAction


class DemoPlanner:
    async def next_action(self, state, capabilities):
        if not state.history:
            return ToolAction(ToolCall(
                tool_name="read_file",
                arguments={"path": "pyproject.toml"},
                hypothesis="The project metadata describes how tests should run.",
                reason="read_file is safer and more precise than terminal/cat.",
            ))
        return FinishAction("Демонстрация завершена: инструмент вызван через registry → policy → executor.")


async def main() -> None:
    registry = build_default_registry()
    executor = ToolExecutor(registry, event_sink=lambda event: print(event))
    runner = AgentRunner(executor, DemoPlanner())
    state = AgentState("Inspect project test configuration", ("pyproject.toml inspected",))
    context = ToolContext(operation_id="demo-op", workspace=Path.cwd(), owner_verified=True)
    print(await runner.run(state, context))


if __name__ == "__main__":
    asyncio.run(main())
