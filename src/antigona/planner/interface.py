"""Planner interface — abstract base for intent-to-plan resolution.

A Planner receives an IntentDecision and returns a structured plan
of actions to be executed. Concrete implementations may use LLM calls,
rule-based expansion, or hybrid approaches.

Day 22: The transport layer MUST NOT call create_flow directly.
All task creation goes through PlannerInterface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from antigona.intent_router import IntentDecision


@dataclass
class PlanResult:
    """Result of planning an IntentDecision into executable actions.

    Attributes:
        flow_id: The created task flow ID (empty if not yet created).
        goal: The resolved goal text.
        tool_name: The resolved tool name.
        command: The resolved shell command (if applicable).
        path: The target path (if applicable).
        content: The content to write (if applicable).
        steps: Plan steps for the executor.
        requires_approval: Whether the plan needs HITL approval.
        error: Error message if planning failed.
    """

    flow_id: str = ""
    goal: str = ""
    tool_name: str = "workspace.write_text"
    command: list[str] = field(default_factory=list)
    path: str = ""
    content: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    requires_approval: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


class PlannerInterface(ABC):
    """Abstract planner that converts user intent into executable task plans.

    The transport layer calls ``plan()`` and then ``execute()`` instead of
    calling the Gateway directly. This is the single entry point for task
    creation from any transport (Telegram, Discord, CLI).
    """

    @abstractmethod
    async def plan(self, decision: IntentDecision, context: dict[str, Any] | None = None) -> PlanResult:
        """Convert an IntentDecision into a plan.

        Args:
            decision: The intent decision from the router.
            context: Optional additional context.

        Returns:
            A PlanResult with the resolved plan details.
        """
        ...

    @abstractmethod
    async def execute(self, plan: PlanResult) -> PlanResult:
        """Execute a plan, typically by creating a flow on the Gateway.

        Args:
            plan: The PlanResult from plan().

        Returns:
            The updated PlanResult with flow_id populated.
        """
        ...


class FlowEnginePlanner(PlannerInterface):
    """Planner that wraps the legacy Gateway-based flow engine.

    This adapter hides the direct ``post_flow()`` call from the transport.
    The transport only sees PlannerInterface methods.
    """

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:8090",
        gateway_token: str = "gateway-token",
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token

    async def _post_flow(
        self,
        goal: str,
        path: str = "task_output.txt",
        content: str = "",
        tool_name: str = "workspace.write_text",
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a flow on the Gateway. Wraps the legacy HTTP call."""
        import time

        import httpx

        headers = {"Authorization": f"Bearer {self.gateway_token}"}
        idempotency_key = f"flow-{int(time.time() * 1000)}"
        headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/flows",
                json={
                    "goal": goal,
                    "path": path,
                    "content": content,
                    "tool_name": tool_name,
                    "command": command or [],
                },
                headers=headers,
            )
            resp.raise_for_status()
            return dict(resp.json())

    async def plan(self, decision: IntentDecision, context: dict[str, Any] | None = None) -> PlanResult:
        """Convert an IntentDecision into a plan.

        Resolves the tool name, path, command, and content from the decision.
        Does NOT create the flow yet — execute() creates it.
        """
        goal = decision.entities.get("goal", "") or (context or {}).get("text", "")
        path = decision.entities.get("path", "task_output.txt")
        content = decision.entities.get("content", goal)
        tool_name = "workspace.write_text"
        command: list[str] = []

        # Planner rule: system executables (for example git) must be installed by
        # the sandbox OS package manager, never by pip. DockerShellTool applies
        # the image-specific apk/apt-get mapping while retaining approval.
        if decision.intent == "task.shell" or goal.startswith("shell:"):
            tool_name = "sandbox.shell"
            cmd_str = goal[6:].strip() if goal.startswith("shell:") else goal
            # Prefer structured argv for simple commands.  Shell syntax is kept
            # behind sh -c and is later subject to strict normalization only;
            # this avoids creating a shell interpretation where none is needed.
            import re
            import shlex
            if not re.search(r"[&|;<>$`\\]|\\n", cmd_str):
                try:
                    command = shlex.split(cmd_str)
                except ValueError:
                    command = ["sh", "-c", cmd_str]
            else:
                command = ["sh", "-c", cmd_str]
            goal = f"Execute shell: {cmd_str}"

        requires_approval = decision.requires_approval

        return PlanResult(
            goal=goal,
            path=path,
            content=content,
            tool_name=tool_name,
            command=command,
            requires_approval=requires_approval,
            steps=[
                {
                    "action": "create_flow",
                    "params": {
                        "goal": goal,
                        "path": path,
                        "content": content,
                        "tool_name": tool_name,
                        "command": command,
                    },
                    "description": f"Create flow for: {goal}",
                }
            ],
        )

    async def execute(self, plan: PlanResult) -> PlanResult:
        """Execute the plan by creating a flow on the Gateway.

        This is the ONLY place in the system that calls the legacy
        Gateway create_flow endpoint.
        """
        if plan.error:
            return plan
        try:
            flow_data = await self._post_flow(
                goal=plan.goal,
                path=plan.path,
                content=plan.content,
                tool_name=plan.tool_name,
                command=plan.command,
            )
            plan.flow_id = str(flow_data.get("id", ""))
            return plan
        except Exception as exc:
            plan.error = str(exc)
            return plan
