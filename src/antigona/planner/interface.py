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
from antigona.task_goal import WRITE_EFFECT_INTENTS, resolve_free_text_request

#: Router intents for which the decision's entities ARE a contract: the intent
#: itself names a workspace read/write tool, so a named path/content is the
#: requested target. Every other intent (conversation, question, a typed tool
#: such as mcp/email/TTS, ``task.code_change``) does NOT own a workspace write:
#: its entities must never turn an effect-free request into a write. See FP-L05d
#: and ``router/intent_router.py::_resolve_bare_verb_with_context``, which copies
#: the PREVIOUS turn's entities into the decision (``last_entities``).
ENTITY_CONTRACT_INTENTS = WRITE_EFFECT_INTENTS | {"task.file_read"}


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
    #: True for a request that asks for NO side effect (conversation/answer, an
    #: unresolvable shell command, a write with no derivable content). Such a
    #: plan creates no flow and can never reach DONE.
    answer_only: bool = False
    #: Generic tool params (e.g. the ``answer_only`` marker).
    params: dict[str, Any] = field(default_factory=dict)

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
        path: str | None = None,
        content: str | None = None,
        tool_name: str | None = None,
        command: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a flow on the Gateway. Wraps the legacy HTTP call.

        FP-L05d: the removed defaults (``path="task_output.txt"``,
        ``tool_name="workspace.write_text"``) are gone. A caller that omits them
        submits FREE TEXT and the Gateway resolves it
        (``resolve_submit_contract``) instead of writing the request text.
        """
        import time

        import httpx

        headers = {"Authorization": f"Bearer {self.gateway_token}"}
        idempotency_key = f"flow-{int(time.time() * 1000)}"
        headers["Idempotency-Key"] = idempotency_key
        payload: dict[str, Any] = {
            "goal": goal,
            "command": command or [],
            "params": params or {},
        }
        if path is not None:
            payload["path"] = path
        if content is not None:
            payload["content"] = content
        if tool_name is not None:
            payload["tool_name"] = tool_name
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/flows",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return dict(resp.json())

    async def plan(self, decision: IntentDecision, context: dict[str, Any] | None = None) -> PlanResult:
        """Convert an IntentDecision into a plan.

        Resolves the tool name, path, command, and content from the canonical
        free-text request resolver (:func:`antigona.task_goal.
        resolve_free_text_request`) — the SAME resolution ``POST /tasks`` uses.
        The old ``content = goal`` default (which made every plan write its own
        request text into a file, the P0 false-DONE machine) is gone: a plan
        only carries content the request actually asked for.
        """
        goal = decision.entities.get("goal", "") or (context or {}).get("text", "")
        request = resolve_free_text_request(goal, decision=decision)

        path = request.path
        content: str = request.content or ""
        tool_name = request.tool_name
        answer_only = request.answer_only
        command = list(request.command)

        # Explicit router entities are a stronger contract than text parsing —
        # but ONLY for an intent that itself owns a workspace read/write tool.
        # ``_resolve_bare_verb_with_context`` fills the decision with the
        # PREVIOUS turn's entities (``last_entities``); letting those reopen a
        # write cleared the resolver's ``answer_only`` and wrote a stale body
        # for a request that asks for no effect at all (FP-L05d).
        entity_path = str(decision.entities.get("path") or "")
        entity_content = decision.entities.get("content")
        entity_contract = decision.intent in ENTITY_CONTRACT_INTENTS
        if decision.intent == "task.shell":
            if command:
                tool_name = "sandbox.shell"
                answer_only = False
        elif entity_contract and (entity_path or entity_content is not None):
            tool_name = (
                "workspace.read_text"
                if decision.intent == "task.file_read"
                else "workspace.write_text"
            )
            answer_only = False
        if entity_contract:
            if entity_path:
                path = entity_path
            if entity_content is not None:
                content = str(entity_content)
        if answer_only:
            # An effect-free plan carries neither a file body nor a target.
            content = ""
            path = ""

        requires_approval = request.requires_approval

        return PlanResult(
            goal=goal,
            path=path,
            content=content,
            tool_name=tool_name,
            command=command,
            requires_approval=requires_approval,
            answer_only=answer_only,
            params={"answer_only": True} if answer_only else {},
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
        if plan.answer_only:
            # Nothing to execute: a request that asks for no side effect must
            # never create a flow. The caller reports the request as a
            # conversation/answer, not as a completed task.
            return plan
        try:
            flow_data = await self._post_flow(
                goal=plan.goal,
                path=plan.path,
                content=plan.content,
                tool_name=plan.tool_name,
                command=plan.command,
                params=plan.params,
            )
            plan.flow_id = str(flow_data.get("id", ""))
            return plan
        except Exception as exc:
            plan.error = str(exc)
            return plan
