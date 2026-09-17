"""TurnEngine — async model→tool→result→model cycle.

The TurnEngine drives an LLM through an OpenAI-compatible API, executing
tool calls and feeding results back until the model produces a final
response or the budget is exhausted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from antigona.turn_bridge.context_adapter import TaskContext, build_system_prompt
from antigona.turn_bridge.message_adapter import message_to_hermes
from antigona.turn_bridge.provider_adapter import (
    ProviderAdapter,
    ProviderAdapterConfig,
    ProviderError,
)
from antigona.turn_bridge.tool_adapter import (
    ToolResponse,
    adapt_tools_for_llm,
    execute_tool_call,
    extract_final_content,
    extract_tool_calls,
    get_finish_reason,
)

LOGGER = logging.getLogger(__name__)


# ── Budget & result types ──────────────────────────────────────────────────


@dataclass
class TurnBudget:
    """Budget constraints for a single ``run_turn`` call.

    Attributes:
        max_turns: Maximum LLM → model round trips.
        max_tool_calls: Maximum total tool invocations.
        max_duration_seconds: Maximum wall-clock time for the entire turn.
    """

    max_turns: int = 30
    max_tool_calls: int = 50
    max_duration_seconds: int = 1800


@dataclass
class TurnResult:
    """Result of a full turn execution.

    Attributes:
        success: Whether the turn completed with a final answer.
        final_response: The model's final text response, or ``None``.
        error: Error message if the turn failed, or ``None``.
        turns_used: Number of LLM round trips performed.
        tool_calls_made: Number of tool invocations performed.
        artifacts: List of artifact dicts produced during the turn.
        messages: The full message history (including tool results).
    """

    success: bool = True
    final_response: str | None = None
    error: str | None = None
    turns_used: int = 0
    tool_calls_made: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


# ── Tool handler map builder ───────────────────────────────────────────────


class TurnEngine:
    """Async turn engine: model → tool_call → tool_result → model → final.

    The engine orchestrates the full LLM interaction cycle:

    1. Send the message history + tool definitions to the provider.
    2. If the response contains tool calls, execute each one.
    3. Feed results back as ``tool``-role messages.
    4. Repeat until the model produces a content response or budget is hit.

    Args:
        provider: A :class:`ProviderAdapter` instance.
        extra_system_prompt: Optional extra system-prompt text prepended
            to every request.
    """

    def __init__(
        self,
        provider: ProviderAdapter,
        extra_system_prompt: str | None = None,
    ) -> None:
        self._provider = provider
        self._extra_system_prompt = extra_system_prompt

    @property
    def provider(self) -> ProviderAdapter:
        return self._provider

    # ── Public API ──────────────────────────────────────────────────────

    async def run_turn(
        self,
        goal: str,
        messages: list[dict[str, Any]],
        available_tools: list[dict[str, Any]],
        budget: TurnBudget | None = None,
        *,
        tool_map: dict[str, Callable[..., Any]] | None = None,
        task_context: TaskContext | None = None,
    ) -> TurnResult:
        """Run the full model → tool → result → model cycle.

        Args:
            goal: High-level goal for this turn.
            messages: Initial message history.
            available_tools: Tool descriptions the model may call.
            budget: Budget constraints (defaults to ``TurnBudget()``).
            tool_map: Mapping of tool name → async callable.  If omitted,
                tool calls are recorded but not executed (simulated).
            task_context: Optional rich :class:`TaskContext` to prepend
                as system context.

        Returns:
            A :class:`TurnResult` describing the outcome.
        """
        effective_budget = budget or TurnBudget()
        tool_map = tool_map or {}
        start_time = time.monotonic()

        # ── Build the working message list ──────────────────────────────
        working_messages = self._build_messages(
            goal=goal,
            messages=messages,
            task_context=task_context,
        )

        llm_tools = adapt_tools_for_llm(available_tools)

        turns_used = 0
        tool_calls_made = 0
        artifacts: list[dict[str, Any]] = []

        while True:
            # ── Check budget ────────────────────────────────────────
            elapsed = time.monotonic() - start_time
            if elapsed > effective_budget.max_duration_seconds:
                return TurnResult(
                    success=False,
                    error=f"Time budget exceeded ({elapsed:.1f}s > {effective_budget.max_duration_seconds}s)",
                    turns_used=turns_used,
                    tool_calls_made=tool_calls_made,
                    artifacts=artifacts,
                    messages=working_messages,
                )

            if turns_used >= effective_budget.max_turns:
                return TurnResult(
                    success=False,
                    error=f"Turn budget exceeded ({turns_used} >= {effective_budget.max_turns})",
                    turns_used=turns_used,
                    tool_calls_made=tool_calls_made,
                    artifacts=artifacts,
                    messages=working_messages,
                )

            # ── Call the LLM ─────────────────────────────────────────
            try:
                response = await self._provider.chat(
                    messages=working_messages,
                    tools=llm_tools if llm_tools else None,
                )
            except ProviderError as exc:
                LOGGER.error("Provider error after %d turns: %s", turns_used, exc)
                return TurnResult(
                    success=False,
                    error=str(exc),
                    turns_used=turns_used,
                    tool_calls_made=tool_calls_made,
                    artifacts=artifacts,
                    messages=working_messages,
                )

            turns_used += 1

            # ── Append assistant message ─────────────────────────────
            choices = response.get("choices", [])
            if not choices:
                return TurnResult(
                    success=False,
                    error="Provider returned no choices",
                    turns_used=turns_used,
                    tool_calls_made=tool_calls_made,
                    artifacts=artifacts,
                    messages=working_messages,
                )

            assistant_msg = choices[0].get("message", {})
            working_messages.append(self._clean_message(assistant_msg))

            # ── Extract tool calls ───────────────────────────────────
            tool_calls = extract_tool_calls(response)

            if not tool_calls:
                # No tool calls → this is a final response
                final = extract_final_content(response)
                finish_reason = get_finish_reason(response)
                LOGGER.info(
                    "Turn complete after %d turns, %d tool calls (finish_reason=%s)",
                    turns_used,
                    tool_calls_made,
                    finish_reason,
                )
                return TurnResult(
                    success=True,
                    final_response=final,
                    turns_used=turns_used,
                    tool_calls_made=tool_calls_made,
                    artifacts=artifacts,
                    messages=working_messages,
                )

            # ── Execute tool calls ───────────────────────────────────
            for call in tool_calls:
                if tool_calls_made >= effective_budget.max_tool_calls:
                    # Inject a budget-exhausted tool result
                    working_messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"Error: Tool call budget exceeded ({tool_calls_made} >= {effective_budget.max_tool_calls})",
                    })
                    # Treat this as a terminal condition
                    return TurnResult(
                        success=False,
                        error=f"Tool call budget exceeded ({tool_calls_made} >= {effective_budget.max_tool_calls})",
                        turns_used=turns_used,
                        tool_calls_made=tool_calls_made,
                        artifacts=artifacts,
                        messages=working_messages,
                    )

                tool_calls_made += 1

                LOGGER.debug("Executing tool call %s: %s(%s)", call.id, call.name, call.arguments)
                tool_response: ToolResponse = await execute_tool_call(call, tool_map)
                LOGGER.debug(
                    "Tool result %s: success=%s, content_len=%d",
                    call.id,
                    tool_response.success,
                    len(tool_response.content),
                )

                # Collect artifacts from ToolExecutionResult-style returns
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_response.content,
                })

                if not tool_response.success:
                    LOGGER.warning("Tool %s failed: %s", call.name, tool_response.content)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _build_messages(
        self,
        goal: str,
        messages: list[dict[str, Any]],
        task_context: TaskContext | None = None,
    ) -> list[dict[str, Any]]:
        """Build the working message list, starting with system context."""
        result: list[dict[str, Any]] = []

        # System prompt
        system_parts: list[str] = []

        if self._extra_system_prompt:
            system_parts.append(self._extra_system_prompt)

        if task_context:
            system_parts.append(build_system_prompt(task_context))
        else:
            system_parts.append(f"# Goal\n\n{goal}")

        if system_parts:
            result.append({
                "role": "system",
                "content": "\n\n".join(system_parts),
            })

        # Existing messages
        for msg in messages:
            result.append(message_to_hermes(msg))

        return result

    @staticmethod
    def _clean_message(msg: dict[str, Any]) -> dict[str, Any]:
        """Ensure a message dict has only expected fields for the provider."""
        clean: dict[str, Any] = {"role": msg.get("role", "assistant")}

        content = msg.get("content")
        if content is not None:
            clean["content"] = content
        elif "tool_calls" in msg:
            clean["content"] = ""  # tool-call-only messages need non-null content

        if "tool_calls" in msg:
            clean["tool_calls"] = msg["tool_calls"]

        return clean


def create_turn_engine(
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout_seconds: int = 120,
    max_retries: int = 2,
    extra_system_prompt: str | None = None,
) -> TurnEngine:
    """Convenience factory for a :class:`TurnEngine` with an embedded provider.

    Args:
        base_url: OpenAI-compatible API base URL.
        api_key: API key.
        model: Model identifier.
        timeout_seconds: Request timeout.
        max_retries: Transient-error retries.
        extra_system_prompt: Optional extra system prompt.

    Returns:
        A configured :class:`TurnEngine`.
    """
    provider = ProviderAdapter(
        config=ProviderAdapterConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
    )
    return TurnEngine(provider=provider, extra_system_prompt=extra_system_prompt)
