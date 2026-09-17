"""Durable Execution Kernel (M1) — executor.

Executes a task's Run. Security boundary: the executor NEVER bypasses
PolicyEngine / ApprovalGrant. A policy denial or a HIGH/CRITICAL action
without a valid, consumed one-shot grant is a structured, non-retryable
failure — the kernel dispatcher is not a privileged backdoor (M0 security
regression guard; P1-002 enforces the ``requires_approval`` flag).

The actual side effect is produced by a pluggable ``handler`` so M1 can focus
on the durable lifecycle; the default handler is a safe no-op that returns a
structured result (the kernel's job is durability + correctness of execution
management, not the tool semantics themselves).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore

logger = logging.getLogger(__name__)

#: (payload, context) -> structured result dict
Handler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


async def _default_handler(
    payload: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    action = context.get("action") or payload.get("tool_name") or "generic"
    return {"output": f"executed {action}", "handler": "default"}


class KernelExecutor:
    """Runs a task payload through the policy boundary, then the handler."""

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        grant_store: ApprovalGrantStore | None = None,
        handler: Handler | None = None,
    ) -> None:
        self.policy_engine = policy_engine or PolicyEngine()
        self.grant_store = grant_store
        self.handler = handler or _default_handler

    def _validate_approval_grant(
        self, token: str, action: str, params: dict[str, Any], actor: str
    ) -> bool:
        """Consume a one-shot ApprovalGrant. Fail-closed: any store error, or a
        missing/expired/consumed grant, denies."""
        if not token or self.grant_store is None:
            return False
        try:
            verdict = self.grant_store.verify_and_consume(
                token,
                actor=actor,
                tool_name=action,
                args=params,
                consumed_by=f"kernel:{actor}",
            )
        except Exception:
            logger.exception("approval grant validation failed (fail-closed)")
            return False
        return bool(verdict.valid)

    async def execute_run(
        self,
        *,
        task: Any,
        actor: str,
        session_id: str = "kernel",
        run_id: str = "",
    ) -> dict[str, Any]:
        """Execute the task payload. Returns a structured result dict.

        Result shape:
            success: bool
            outcome: "SUCCEEDED" | "POLICY_DENIED" | "APPROVAL_REQUIRED" | "FAILED"
            result: dict (on success)
            error: {code, message, retryable} (on failure)
        """
        from antigona.observability_legacy import record as _legacy_record
        _legacy_record("kernel.executor.execute_run")
        payload = dict(task.payload or {})
        action = str(payload.get("tool_name") or task.kind or "generic")
        params = {k: v for k, v in payload.items() if k not in ("approval_token",)}
        context = {
            "channel": "kernel",
            "user_id": actor,
            "session_id": session_id,
            "task_id": task.id,
            "run_id": run_id,
        }

        # 1. Policy boundary — never bypassed.
        try:
            verdict = await self.policy_engine.check(
                action, params=params, context=context
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed at the boundary
            return {
                "success": False,
                "outcome": "POLICY_DENIED",
                "error": {
                    "code": "POLICY_INTERNAL_ERROR",
                    "message": f"policy evaluation failed: {exc}",
                    "retryable": False,
                },
            }
        if not verdict.get("allowed", False):
            # P1-002: enforce the requires_approval flag (HIGH) as well as the
            # CRITICAL 2-step path. INTERNAL_ERROR / missing-identity denials
            # keep requires_approval=True but risk_level UNKNOWN → POLICY_DENIED.
            risk = str(verdict.get("risk_level") or "").upper()
            needs_grant = bool(verdict.get("requires_2step_confirmation")) or (
                bool(verdict.get("requires_approval")) and risk in {"HIGH", "CRITICAL"}
            )
            if needs_grant:
                token = str(payload.get("approval_token") or "")
                if not self._validate_approval_grant(token, action, params, actor):
                    return {
                        "success": False,
                        "outcome": "APPROVAL_REQUIRED",
                        "error": {
                            "code": "APPROVAL_REQUIRED",
                            "message": (
                                "action requires a valid one-shot approval "
                                "grant; none provided or grant already consumed"
                            ),
                            "retryable": False,
                        },
                    }
                # fall through to execute with the consumed grant
            else:
                return {
                    "success": False,
                    "outcome": "POLICY_DENIED",
                    "error": {
                        "code": "POLICY_DENIED",
                        "message": verdict.get("reason", "action denied by policy"),
                        "retryable": False,
                    },
                }

        # 3. Actual side effect via the pluggable handler.
        try:
            result = await self.handler(params, context)
            return {"success": True, "outcome": "SUCCEEDED", "result": result}
        except Exception as exc:  # noqa: BLE001 — a handler bug is a retryable failure
            logger.exception("kernel run handler failed for action=%s", action)
            return {
                "success": False,
                "outcome": "FAILED",
                "error": {
                    "code": "EXEC_ERROR",
                    "message": str(exc),
                    "retryable": True,
                },
            }
