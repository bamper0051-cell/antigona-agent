"""Tool adapter — format tools for the LLM and execute tool calls.

Converts between the Antigona tool-description format and provider-format
tool definitions (OpenAI ``tools`` array), executes tools, and packages
results back into ``tool``-role messages the LLM can consume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from antigona.durable.execution_models import ToolExecutionResult

LOGGER = logging.getLogger(__name__)


# ── Data classes ───────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """A single tool call requested by the LLM.

    Attributes:
        id: Unique identifier from the provider (``call_xxxx``).
        name: Name of the tool to invoke.
        arguments: Parsed keyword arguments for the tool.
    """

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }


@dataclass
class ToolResponse:
    """A tool-response message to feed back to the LLM.

    Attributes:
        tool_call_id: The id of the :class:`ToolCall` this responds to.
        content: The result content (stringified).
        success: Whether execution succeeded.
        error_type: Machine-readable error type, if any.
    """

    tool_call_id: str
    content: str
    success: bool = True
    error_type: str | None = None


# ── Tool format conversion ─────────────────────────────────────────────────


def adapt_tools_for_llm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Antigona tool descriptions to the provider ``tools`` format.

    The Antigona format uses ``name``, ``description``, and ``parameters``
    keys, which is already OpenAI-compatible.  This function validates and
    normalises the entries.

    Args:
        tools: List of tool descriptions, each with at minimum ``name``.

    Returns:
        List of tool dicts in OpenAI provider format.
    """
    result: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name", "")
        if not name:
            LOGGER.warning("Skipping tool without name: %s", tool)
            continue
        entry: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        result.append(entry)
    return result


# ── Tool call extraction ───────────────────────────────────────────────────


def extract_tool_calls(response: dict[str, Any]) -> list[ToolCall]:
    """Extract tool calls from a provider chat-completion response.

    Args:
        response: The parsed JSON response from the provider.

    Returns:
        List of :class:`ToolCall` instances, empty if none.
    """
    choices = response.get("choices", [])
    if not choices:
        return []

    message = choices[0].get("message", {})
    raw_calls = message.get("tool_calls") or []

    calls: list[ToolCall] = []
    for raw in raw_calls:
        if raw.get("type") != "function":
            continue
        fn = raw.get("function", {})
        raw_args = fn.get("arguments", "{}")
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            LOGGER.warning("Failed to parse tool arguments JSON: %s", raw_args[:200])
            parsed_args = {"_raw": raw_args}

        calls.append(
            ToolCall(
                id=raw.get("id", ""),
                name=fn.get("name", ""),
                arguments=parsed_args,
            )
        )
    return calls


# ── Tool execution ─────────────────────────────────────────────────────────


async def execute_tool_call(
    call: ToolCall,
    tool_map: dict[str, Any],
) -> ToolResponse:
    """Execute a single tool call and produce a :class:`ToolResponse`.

    The ``tool_map`` should map tool names to callables that accept
    ``**kwargs`` and return either a string, a dict, or a
    :class:`ToolExecutionResult`.

    Args:
        call: The tool call to execute.
        tool_map: Mapping of tool name → executable.

    Returns:
        A ToolResponse ready to feed back to the LLM.
    """
    handler = tool_map.get(call.name)
    if handler is None:
        LOGGER.error("Unknown tool: %s (available: %s)", call.name, list(tool_map.keys()))
        return ToolResponse(
            tool_call_id=call.id,
            content=f"Error: unknown tool '{call.name}'",
            success=False,
            error_type="tool_not_found",
        )

    try:
        if call.arguments:
            result = await handler(**call.arguments)
        else:
            result = await handler()
    except Exception as exc:
        LOGGER.exception("Tool %s failed", call.name)
        return ToolResponse(
            tool_call_id=call.id,
            content=f"Error: {type(exc).__name__}: {exc}",
            success=False,
            error_type=type(exc).__name__,
        )

    return _format_tool_result(call.id, result)


def _format_tool_result(tool_call_id: str, result: Any) -> ToolResponse:
    """Format a raw tool result into a :class:`ToolResponse`."""
    if isinstance(result, ToolExecutionResult):
        if result.error_message:
            return ToolResponse(
                tool_call_id=tool_call_id,
                content=result.error_message,
                success=result.technical_success,
                error_type=result.error_type,
            )
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        if result.artifacts:
            output_parts.append(f"[artifacts: {json.dumps(result.artifacts)}]")
        content = "\n".join(output_parts) if output_parts else "(empty result)"
        return ToolResponse(
            tool_call_id=tool_call_id,
            content=content,
            success=True,
        )

    if isinstance(result, dict):
        return ToolResponse(
            tool_call_id=tool_call_id,
            content=json.dumps(result, ensure_ascii=False, default=str),
            success=True,
        )

    return ToolResponse(
        tool_call_id=tool_call_id,
        content=str(result),
        success=True,
    )


def extract_final_content(response: dict[str, Any]) -> str | None:
    """Extract the assistant's text content from a provider response.

    Returns ``None`` if the response contains tool calls instead of text,
    or if no choices exist.
    """
    choices = response.get("choices", [])
    if not choices:
        return None
    message = choices[0].get("message", {})
    if message.get("tool_calls"):
        return None  # No final content — tool calls to execute
    content = message.get("content")
    if content:
        return str(content)
    return None


def get_finish_reason(response: dict[str, Any]) -> str | None:
    """Extract the finish reason from a provider response."""
    choices = response.get("choices", [])
    if not choices:
        return None
    return cast(str | None, choices[0].get("finish_reason"))
