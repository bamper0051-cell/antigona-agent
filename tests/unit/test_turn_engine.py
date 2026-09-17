"""Regression tests for the TurnEngine.

Tests cover:
- Basic turn with tool call -> execution -> result -> final
- Tool result feedback loop
- Budget exhaustion (max_turns)
- Error classification (rate limit, timeout)
- Empty provider response
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from antigona.turn_bridge import TurnBudget, TurnEngine
from antigona.turn_bridge.context_adapter import TaskContext
from antigona.turn_bridge.provider_adapter import (
    ErrorCategory,
    ProviderAdapter,
    ProviderAdapterConfig,
    ProviderError,
    classify_error,
)
from antigona.turn_bridge.tool_adapter import (
    ToolCall,
    adapt_tools_for_llm,
    execute_tool_call,
    extract_final_content,
    extract_tool_calls,
    get_finish_reason,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_provider_response(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
) -> dict:
    """Build a fake provider chat-completion response."""
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    else:
        message["content"] = content or ""

    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _make_tool_call(id: str, name: str, args: dict) -> dict:
    """Build a tool-call dict matching the provider format."""
    return {
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


# ── Provider adapter tests ─────────────────────────────────────────────────


class TestClassifyError:
    """Section: error classification logic."""

    def test_rate_limit_429(self) -> None:
        exc = ProviderError(ErrorCategory.RATE_LIMIT, "rate limit", status_code=429)
        classified = classify_error(exc, response_body="rate limit exceeded")
        assert classified.category == ErrorCategory.RATE_LIMIT
        assert classified.retryable

    def test_timeout(self) -> None:
        exc = TimeoutError("connection timed out")
        classified = classify_error(exc)
        assert classified.category == ErrorCategory.TIMEOUT
        assert classified.retryable

    def test_auth_error(self) -> None:
        exc = ProviderError(ErrorCategory.AUTH_ERROR, "invalid API key", status_code=401)
        classified = classify_error(exc, response_body="unauthorized")
        assert classified.category == ErrorCategory.AUTH_ERROR
        assert not classified.retryable

    def test_context_length(self) -> None:
        exc = ProviderError(ErrorCategory.CONTEXT_LENGTH, "context_length_exceeded", status_code=400)
        classified = classify_error(exc, response_body="maximum context length")
        assert classified.category == ErrorCategory.CONTEXT_LENGTH
        assert not classified.retryable

    def test_overloaded_503(self) -> None:
        exc = ProviderError(ErrorCategory.OVERLOADED, "overloaded", status_code=503)
        classified = classify_error(exc, response_body="service unavailable")
        assert classified.category == ErrorCategory.OVERLOADED
        assert classified.retryable

    def test_unknown_error(self) -> None:
        exc = RuntimeError("something weird happened")
        classified = classify_error(exc)
        assert classified.category == ErrorCategory.UNKNOWN
        assert not classified.retryable


# ── Tool adapter tests ──────────────────────────────────────────────────────


class TestAdaptToolsForLLM:
    """Section: tool adaptation."""

    def test_basic_conversion(self) -> None:
        antigona_tools = [
            {
                "name": "calculator",
                "description": "Evaluate math",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                    "required": ["expr"],
                },
            }
        ]
        result = adapt_tools_for_llm(antigona_tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "calculator"

    def test_skips_tool_without_name(self) -> None:
        result = adapt_tools_for_llm([{"description": "no name"}])
        assert len(result) == 0

    def test_empty_list(self) -> None:
        assert adapt_tools_for_llm([]) == []


class TestExtractToolCalls:
    """Section: tool call extraction."""

    def test_no_tool_calls(self) -> None:
        response = _make_provider_response(content="Hello")
        assert extract_tool_calls(response) == []

    def test_single_tool_call(self) -> None:
        tc = _make_tool_call("call_1", "get_weather", {"city": "London"})
        response = _make_provider_response(tool_calls=[tc])
        calls = extract_tool_calls(response)
        assert len(calls) == 1
        assert calls[0].id == "call_1"
        assert calls[0].name == "get_weather"
        assert calls[0].arguments == {"city": "London"}

    def test_multiple_tool_calls(self) -> None:
        tc1 = _make_tool_call("call_1", "tool_a", {"x": 1})
        tc2 = _make_tool_call("call_2", "tool_b", {"y": 2})
        response = _make_provider_response(tool_calls=[tc1, tc2])
        calls = extract_tool_calls(response)
        assert len(calls) == 2

    def test_malformed_json_arguments(self) -> None:
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "bad_tool",
                "arguments": "not valid json",
            },
        }
        response = _make_provider_response(tool_calls=[tc])
        calls = extract_tool_calls(response)
        assert len(calls) == 1
        assert "_raw" in calls[0].arguments


class TestExtractFinalContent:
    """Section: final content extraction."""

    def test_text_response(self) -> None:
        response = _make_provider_response(content="Final answer: 42")
        assert extract_final_content(response) == "Final answer: 42"

    def test_tool_call_response(self) -> None:
        tc = _make_tool_call("c1", "calc", {"expr": "42"})
        response = _make_provider_response(tool_calls=[tc])
        assert extract_final_content(response) is None

    def test_empty_choices(self) -> None:
        assert extract_final_content({}) is None


class TestGetFinishReason:
    """Section: finish reason extraction."""

    def test_stop_reason(self) -> None:
        response = _make_provider_response(content="done")
        assert get_finish_reason(response) == "stop"

    def test_tool_calls_reason(self) -> None:
        tc = _make_tool_call("c1", "tool", {})
        response = _make_provider_response(tool_calls=[tc], finish_reason="tool_calls")
        assert get_finish_reason(response) == "tool_calls"

    def test_no_choices(self) -> None:
        assert get_finish_reason({}) is None


class TestExecuteToolCall:
    """Section: tool call execution."""

    async def test_calls_handler(self) -> None:
        async def my_tool(**kwargs: object) -> str:
            return f"result: {kwargs}"

        call = ToolCall(id="c1", name="my_tool", arguments={"x": 42})
        result = await execute_tool_call(call, {"my_tool": my_tool})
        assert result.success
        assert "result:" in result.content

    async def test_unknown_tool(self) -> None:
        call = ToolCall(id="c1", name="unknown_tool", arguments={})
        result = await execute_tool_call(call, {})
        assert not result.success
        assert "unknown tool" in result.content.lower()
        assert result.error_type == "tool_not_found"

    async def test_tool_raises_exception(self) -> None:
        async def failing_tool(**kwargs: object) -> str:
            raise ValueError("something broke")

        call = ToolCall(id="c1", name="failing_tool", arguments={})
        result = await execute_tool_call(call, {"failing_tool": failing_tool})
        assert not result.success
        assert "ValueError" in result.content


# ── Context adapter tests ──────────────────────────────────────────────────


class TestTaskContext:
    """Section: task context building."""

    def test_minimal_context(self) -> None:
        from antigona.turn_bridge.context_adapter import prepare_task_context

        msgs = prepare_task_context("Do something")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
        assert "Do something" in msgs[0]["content"]

    def test_full_context(self) -> None:
        from antigona.turn_bridge.context_adapter import prepare_task_context

        msgs = prepare_task_context(
            goal="Build API",
            acceptance_criteria=["Works", "Fast"],
            current_plan="Step 1",
            evidence={"file": "main.py"},
            facts=["Python 3.11+"],
        )
        assert len(msgs) >= 1
        assert any("Works" in m.get("content", "") for m in msgs)

    def test_task_context_dataclass(self) -> None:
        ctx = TaskContext(
            goal="Test goal",
            acceptance_criteria=["C1"],
            facts=["Fact 1"],
        )
        from antigona.turn_bridge.context_adapter import build_system_prompt

        prompt = build_system_prompt(ctx)
        assert "Test goal" in prompt
        assert "C1" in prompt
        assert "Fact 1" in prompt


# ── TurnEngine tests ───────────────────────────────────────────────────────


class TestTurnEngine:
    """Section: full TurnEngine cycle."""

    async def _make_mock_provider(self, responses: list[dict]) -> ProviderAdapter:
        """Create a provider that returns canned responses."""
        mock = AsyncMock(spec=ProviderAdapter)
        mock.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock.chat = AsyncMock(side_effect=responses)
        return mock

    async def test_basic_turn_with_tool_call(self) -> None:
        """Model → tool_call → execute → result → final response."""
        tc1 = _make_tool_call("call_1", "multiply", {"a": 6, "b": 7})
        response1 = _make_provider_response(tool_calls=[tc1], finish_reason="tool_calls")
        response2 = _make_provider_response(content="The answer is 42.")

        provider = await self._make_mock_provider([response1, response2])
        engine = TurnEngine(provider=provider)

        async def multiply(**kwargs: object) -> str:
            a = int(kwargs.get("a", 0))  # type: ignore[arg-type]
            b = int(kwargs.get("b", 0))  # type: ignore[arg-type]
            return str(a * b)

        result = await engine.run_turn(
            goal="Calculate 6 * 7",
            messages=[{"role": "user", "content": "What is 6 * 7?"}],
            available_tools=[
                {
                    "name": "multiply",
                    "description": "Multiply two numbers",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "required": ["a", "b"],
                    },
                }
            ],
            budget=TurnBudget(max_turns=5, max_tool_calls=10, max_duration_seconds=30),
            tool_map={"multiply": multiply},
        )

        assert result.success
        assert result.final_response == "The answer is 42."
        assert result.turns_used == 2
        assert result.tool_calls_made == 1
        assert len(result.messages) >= 4  # system + user + assistant(tc) + tool + assistant(final)

    async def test_tool_result_feedback(self) -> None:
        """Tool result is fed back to the model in the next turn."""
        tc1 = _make_tool_call("call_1", "echo", {"text": "hello"})
        response1 = _make_provider_response(tool_calls=[tc1], finish_reason="tool_calls")
        response2 = _make_provider_response(content="Echo received.")

        provider = await self._make_mock_provider([response1, response2])
        engine = TurnEngine(provider=provider)

        async def echo(**kwargs: object) -> str:
            return f"echoed: {kwargs['text']}"

        result = await engine.run_turn(
            goal="Test echo",
            messages=[{"role": "user", "content": "echo hello"}],
            available_tools=[{
                "name": "echo",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }],
            tool_map={"echo": echo},
        )

        assert result.success
        assert result.tool_calls_made == 1

        # Verify the tool result was in the messages passed to the second call
        _, tool_msgs = provider.chat.call_args_list[1]
        messages = tool_msgs.get("messages", tool_msgs.get("kwargs", {}).get("messages", []))
        # Find tool-role messages
        tool_role_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_role_msgs) == 1
        assert "echoed:" in tool_role_msgs[0].get("content", "")

    async def test_budget_exhaustion_max_turns(self) -> None:
        """Engine stops when max_turns is hit."""
        tc1 = _make_tool_call("call_1", "echo", {"text": "x"})
        response_tc = _make_provider_response(tool_calls=[tc1], finish_reason="tool_calls")

        # Always returns tool_calls — will exhaust budget
        provider = await self._make_mock_provider([response_tc] * 10)
        engine = TurnEngine(provider=provider)

        async def echo(**kwargs: object) -> str:
            return f"echoed: {kwargs.get('text', '')}"

        result = await engine.run_turn(
            goal="Loop test",
            messages=[{"role": "user", "content": "keep going"}],
            available_tools=[{
                "name": "echo",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }],
            budget=TurnBudget(max_turns=3, max_tool_calls=10, max_duration_seconds=30),
            tool_map={"echo": echo},
        )

        assert not result.success
        assert "Turn budget exceeded" in (result.error or "")
        assert result.turns_used == 3

    async def test_budget_exhaustion_max_tool_calls(self) -> None:
        """Engine stops when max_tool_calls is hit (multiple tools per turn)."""
        tc1 = _make_tool_call("call_1", "echo", {"text": "a"})
        tc2 = _make_tool_call("call_2", "echo", {"text": "b"})
        response_tc = _make_provider_response(tool_calls=[tc1, tc2], finish_reason="tool_calls")

        provider = await self._make_mock_provider([response_tc])
        engine = TurnEngine(provider=provider)

        async def echo(**kwargs: object) -> str:
            return f"echoed: {kwargs.get('text', '')}"

        result = await engine.run_turn(
            goal="Tool budget test",
            messages=[{"role": "user", "content": "call tools"}],
            available_tools=[{
                "name": "echo",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }],
            budget=TurnBudget(max_turns=10, max_tool_calls=1, max_duration_seconds=30),
            tool_map={"echo": echo},
        )

        assert not result.success
        assert "Tool call budget exceeded" in (result.error or "")

    async def test_empty_response(self) -> None:
        """Provider returns empty choices list."""
        provider = await self._make_mock_provider([{"object": "chat.completion", "choices": []}])
        engine = TurnEngine(provider=provider)

        result = await engine.run_turn(
            goal="Empty test",
            messages=[{"role": "user", "content": "hello"}],
            available_tools=[],
        )

        assert not result.success
        assert "no choices" in (result.error or "").lower()
        assert result.turns_used == 1

    async def test_provider_error(self) -> None:
        """Provider raises an error."""
        mock = AsyncMock(spec=ProviderAdapter)
        mock.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock.chat = AsyncMock(side_effect=ProviderError(
            category=ErrorCategory.RATE_LIMIT,
            message="Rate limited",
            status_code=429,
            retryable=True,
        ))

        engine = TurnEngine(provider=mock)

        result = await engine.run_turn(
            goal="Error test",
            messages=[{"role": "user", "content": "hello"}],
            available_tools=[],
        )

        assert not result.success
        assert "Rate limited" in (result.error or "")
        assert result.turns_used == 0

    async def test_time_budget_exhaustion(self) -> None:
        """Engine stops when duration budget is exceeded."""
        tc1 = _make_tool_call("call_1", "sleep_tool", {"seconds": 5})
        response_tc = _make_provider_response(tool_calls=[tc1], finish_reason="tool_calls")

        provider = await self._make_mock_provider([response_tc])
        engine = TurnEngine(provider=provider)

        async def sleep_tool(**kwargs: object) -> str:
            import asyncio
            secs = int(kwargs.get("seconds", 1))  # type: ignore[arg-type]
            await asyncio.sleep(secs)
            return "done"

        result = await engine.run_turn(
            goal="Timeout test",
            messages=[{"role": "user", "content": "sleep"}],
            available_tools=[{
                "name": "sleep_tool",
                "description": "Sleep",
                "parameters": {
                    "type": "object",
                    "properties": {"seconds": {"type": "integer"}},
                    "required": ["seconds"],
                },
            }],
            budget=TurnBudget(max_turns=10, max_tool_calls=10, max_duration_seconds=1),
            tool_map={"sleep_tool": sleep_tool},
        )

        assert not result.success
        assert "Time budget exceeded" in (result.error or "")
        assert result.turns_used == 1

    async def test_extra_system_prompt(self) -> None:
        """Extra system prompt is prepended."""
        mock = AsyncMock(spec=ProviderAdapter)
        mock.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock.chat = AsyncMock(return_value=_make_provider_response(content="Done."))

        engine = TurnEngine(provider=mock, extra_system_prompt="You are a helpful assistant.")
        result = await engine.run_turn(
            goal="Test",
            messages=[{"role": "user", "content": "hi"}],
            available_tools=[],
        )

        assert result.success
        # Check the system prompt was included
        call_args, call_kwargs = mock.chat.call_args
        messages = call_kwargs.get("messages", [])
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) >= 1
        assert "helpful assistant" in system_msgs[0].get("content", "")

    async def test_no_tool_map_executes_nothing(self) -> None:
        """Tool calls with no tool_map are logged but not executed."""
        tc1 = _make_tool_call("call_1", "missing_tool", {})
        response = _make_provider_response(tool_calls=[tc1], finish_reason="tool_calls")

        mock = AsyncMock(spec=ProviderAdapter)
        mock.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock.chat = AsyncMock(return_value=response)

        engine = TurnEngine(provider=mock)
        result = await engine.run_turn(
            goal="Test no map",
            messages=[{"role": "user", "content": "go"}],
            available_tools=[{"name": "missing_tool", "description": "x"}],
            tool_map={},
            budget=TurnBudget(max_turns=1, max_tool_calls=5, max_duration_seconds=10),
        )

        # The tool execution with no map should produce a "tool_not_found" response
        # and then the turn ends because the response showed tool_calls but there's
        # no second call — actually the engine only returns after the next provider call
        # So we need to check that turns_used == 1 because the loop only breaks
        # when tool_calls are empty or budget exhausted
        assert result.turns_used == 1
        assert not result.success  # no follow-up call with content

    async def test_task_context_injection(self) -> None:
        """TaskContext is injected into the system prompt."""
        mock = AsyncMock(spec=ProviderAdapter)
        mock.config = ProviderAdapterConfig(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        mock.chat = AsyncMock(return_value=_make_provider_response(content="Done."))

        engine = TurnEngine(provider=mock)
        ctx = TaskContext(
            goal="Custom goal",
            acceptance_criteria=["Must work"],
            facts=["Python is used"],
        )

        await engine.run_turn(
            goal="Custom goal",
            messages=[],
            available_tools=[],
            task_context=ctx,
        )

        call_args, call_kwargs = mock.chat.call_args
        messages = call_kwargs.get("messages", [])
        system_msgs = [m for m in messages if m.get("role") == "system"]
        combined = " ".join(m.get("content", "") for m in system_msgs)
        assert "Custom goal" in combined
        assert "Must work" in combined
        assert "Python is used" in combined


# ── Message adapter tests ──────────────────────────────────────────────────


class TestMessageAdapter:
    """Section: message format conversion."""

    def test_message_to_provider_basic(self) -> None:
        from antigona.turn_bridge.message_adapter import message_to_hermes

        result = message_to_hermes({"role": "user", "content": "Hello"})
        assert result == {"role": "user", "content": "Hello"}

    def test_message_to_provider_tool_call(self) -> None:
        from antigona.turn_bridge.message_adapter import message_to_hermes

        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x"}}],
        }
        result = message_to_hermes(msg)
        assert result["role"] == "assistant"
        assert "tool_calls" in result

    def test_message_to_provider_tool_result(self) -> None:
        from antigona.turn_bridge.message_adapter import message_to_hermes

        msg = {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "Result here",
        }
        result = message_to_hermes(msg)
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "c1"

    def test_message_from_provider(self) -> None:
        from antigona.turn_bridge.message_adapter import message_from_hermes

        result = message_from_hermes({"role": "assistant", "content": "Hello"})
        assert result["role"] == "assistant"
        assert result["content"] == "Hello"


# ── create_turn_engine factory ─────────────────────────────────────────────


class TestCreateTurnEngine:
    """Section: factory function."""

    def test_create_turn_engine(self) -> None:
        from antigona.turn_bridge.turn_engine_adapter import create_turn_engine

        engine = create_turn_engine(
            base_url="https://test.local/v1",
            api_key="test-key",
            model="test-model",
            timeout_seconds=30,
            max_retries=1,
            extra_system_prompt="Be helpful",
        )
        assert engine is not None
        assert engine.provider.config.model == "test-model"
