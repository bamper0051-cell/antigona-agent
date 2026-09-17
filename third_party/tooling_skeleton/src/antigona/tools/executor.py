from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Sequence

from .contracts import ToolCall, ToolCallRecord, ToolContext, ToolResult, ToolStatus
from .events import ToolEvent
from .policy import ToolPolicyEngine
from .registry import ToolRegistry
from .schema import validate_arguments


EventSink = Callable[[ToolEvent], None]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicyEngine | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or ToolPolicyEngine()
        self.event_sink = event_sink

    def _emit(self, event: ToolEvent) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event)
        except Exception:
            pass

    async def execute(
        self,
        call: ToolCall,
        context: ToolContext,
        history: Sequence[ToolCallRecord] = (),
    ) -> ToolResult:
        spec = self.registry.get(call.tool_name)
        if spec is None:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.UNAVAILABLE,
                summary=f"Unknown tool: {call.tool_name}",
                error_type="UNKNOWN_TOOL",
            )

        check = spec.availability_check
        try:
            available = check is None or check(context)
        except Exception as exc:
            available = False
            availability_error = type(exc).__name__
        else:
            availability_error = None
        if not available:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.UNAVAILABLE,
                summary="Tool is unavailable in the current capability snapshot.",
                error_type=availability_error or "UNAVAILABLE",
                retryable=True,
            )

        errors = validate_arguments(spec.schema_for(context), call.arguments)
        if errors:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.INVALID_ARGUMENTS,
                summary="; ".join(errors),
                error_type="INVALID_ARGUMENTS",
            )

        decision = self.policy.authorize(spec, call, context, history)
        if not decision.allowed:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.DENIED,
                summary=decision.reason,
                error_type=decision.code,
            )

        self._emit(ToolEvent(
            "tool.started",
            context.operation_id,
            call.call_id,
            call.tool_name,
            {"hypothesis": call.hypothesis, "reason": call.reason},
        ))
        started = time.monotonic()

        try:
            result_or_awaitable = spec.handler(call.arguments, context)
            if inspect.isawaitable(result_or_awaitable):
                result = await asyncio.wait_for(result_or_awaitable, timeout=spec.timeout_seconds)
            else:
                result = result_or_awaitable
        except asyncio.TimeoutError:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.TIMEOUT,
                summary=f"Tool exceeded {spec.timeout_seconds:g}s timeout.",
                error_type="TIMEOUT",
                retryable=True,
            )
        except asyncio.CancelledError:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.CANCELLED,
                summary="Tool execution was cancelled.",
                error_type="CANCELLED",
                retryable=True,
            )
        except Exception as exc:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=ToolStatus.ERROR,
                summary=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
                retryable=False,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        if result.call_id != call.call_id or result.tool_name != call.tool_name or result.duration_ms == 0:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=result.status,
                summary=result.summary,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                error_type=result.error_type,
                retryable=result.retryable,
                duration_ms=duration_ms,
                truncated=result.truncated,
                artifacts=result.artifacts,
                data=result.data,
            )

        self._emit(ToolEvent(
            "tool.finished",
            context.operation_id,
            call.call_id,
            call.tool_name,
            {"status": result.status, "summary": result.summary, "duration_ms": result.duration_ms},
        ))
        return result
