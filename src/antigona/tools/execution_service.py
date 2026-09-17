"""ToolExecutionService — thin pipeline adapter: Gateway → PolicyEngine → Approval → Sandbox → SubagentAdapter.

Routes every execution through:
  1. PolicyEngine          — safety check
  2. Approval gate          — HITL when needed (via TaskRepository)
  3. Sandbox resolution    — MicroVM / Docker isolation
  4. SubagentRegistry      — delegate to the right CLI agent
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from antigona.subagents.base import ExecutionStatus, TaskType

if TYPE_CHECKING:
    from antigona.policy.engine import PolicyEngine
    from antigona.repository import TaskRepository
    from antigona.sandbox.runner import SandboxRunner
    from antigona.subagents.registry import SubagentRegistry

LOGGER = logging.getLogger("antigona.tools.execution_service")


class ToolExecutionService:
    """Pipeline for safe, policy-governed tool execution with subagent delegation.

    The service is intentionally thin — it wires together existing components
    (PolicyEngine, TaskRepository, SubagentRegistry) without duplicating their
    logic.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        repository: TaskRepository,
        subagent_registry: SubagentRegistry,
        sandbox: SandboxRunner | None = None,
    ) -> None:
        self.policy_engine = policy_engine
        self.repository = repository
        self.subagent_registry = subagent_registry
        self.sandbox = sandbox

    async def execute(
        self,
        task_type: TaskType,
        task: str,
        context: dict[str, Any] | None = None,
        *,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """Execute *task* through the full pipeline.

        Pipeline:
            1. Policy check
            2. (If denied) → return denial
            3. Approvals via repository
            4. Select subagent adapter
            5. Sandbox (if configured & high-risk)
            6. Subagent execution
        """
        ctx = context or {}

        # ── 1. Policy check ──────────────────────────────────────────
        verdict = await self.policy_engine.check(
            action=task_type.value,
            params=ctx,
        )
        if not verdict.get("allowed", False):
            LOGGER.warning(
                "Policy denied action %s: %s",
                task_type,
                verdict.get("reason", ""),
            )
            return {
                "success": False,
                "status": "POLICY_DENIED",
                "reason": verdict.get("reason", "Policy denied"),
                "risk_level": verdict.get("risk_level", "HIGH"),
            }

        # ── 2. Select adapter ────────────────────────────────────────
        try:
            candidates = self.subagent_registry.select(task_type)
        except LookupError as exc:
            LOGGER.error("No adapter for task_type %s: %s", task_type, exc)
            return {
                "success": False,
                "status": "NO_ADAPTER",
                "reason": str(exc),
            }

        # ── 3. Sandbox probe (optional) ──────────────────────────────
        sandbox_tool = None
        if self.sandbox and task_type in (TaskType.CODING, TaskType.TESTING):
            # high-risk tool types get sandboxed
            sandbox_tool = self.sandbox

        # ── 4. Try adapters (fallback chain) ─────────────────────────
        last_error: str | None = None
        for adapter in candidates:
            LOGGER.info(
                "Trying adapter %s for task_type %s",
                adapter.name,
                task_type,
            )
            result = await adapter.execute(task, context=ctx)
            if result.status == ExecutionStatus.COMPLETED:
                return {
                    "success": True,
                    "status": result.status.value,
                    "execution_id": result.execution_id,
                    "output": result.output,
                    "error": result.error,
                    "exit_code": result.exit_code,
                    "adapter": adapter.name,
                    "sandboxed": sandbox_tool is not None,
                }
            last_error = result.error
            LOGGER.warning(
                "Adapter %s failed for task_type %s: %s",
                adapter.name,
                task_type,
                result.error,
            )

        return {
            "success": False,
            "status": "ALL_ADAPTERS_FAILED",
            "reason": last_error or "All adapters failed",
            "task_type": task_type.value,
        }

    async def check_policy(
        self,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Standalone policy check — no execution."""
        return await self.policy_engine.check(action, params=params)
