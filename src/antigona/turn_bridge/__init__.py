"""Hermes-derived TurnEngine — async model→tool→result→model cycle.

This package provides a self-contained TurnEngine that drives an LLM through
an OpenAI-compatible chat-completion API, executing tool calls and feeding
results back until the model produces a final answer or a budget limit is hit.

Usage::

    from antigona.turn_bridge import TurnEngine, TurnBudget, TurnResult

    engine = TurnEngine(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o",
    )
    result = await engine.run_turn(
        goal="Calculate 42 * 7",
        messages=[{"role": "user", "content": "What is 42 * 7?"}],
        available_tools=[
            {
                "name": "calculator",
                "description": "Evaluate a math expression",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expr": {"type": "string"}
                    },
                    "required": ["expr"],
                },
            }
        ],
        budget=TurnBudget(max_turns=10, max_tool_calls=20, max_duration_seconds=60),
    )
    print(result.final_response)
"""

from __future__ import annotations

from antigona.turn_bridge.context_adapter import prepare_task_context
from antigona.turn_bridge.message_adapter import message_from_hermes, message_to_hermes
from antigona.turn_bridge.provider_adapter import (
    ErrorCategory,
    ProviderAdapter,
    ProviderAdapterConfig,
    ProviderError,
    classify_error,
)
from antigona.turn_bridge.tool_adapter import (
    ToolCall,
    ToolResponse,
    adapt_tools_for_llm,
    execute_tool_call,
)
from antigona.turn_bridge.turn_engine_adapter import TurnBudget, TurnEngine, TurnResult

__all__ = [
    "TurnBudget",
    "TurnEngine",
    "TurnResult",
    "message_to_hermes",
    "message_from_hermes",
    "ToolCall",
    "ToolResponse",
    "adapt_tools_for_llm",
    "execute_tool_call",
    "prepare_task_context",
    "ErrorCategory",
    "ProviderAdapter",
    "ProviderAdapterConfig",
    "ProviderError",
    "classify_error",
]

__version__ = "0.1.0"
