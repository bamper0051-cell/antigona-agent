"""Day 22 tests: Planner adapter and gate check.

Verifies:
- FlowEnginePlanner correctly wraps legacy flow creation
- Transport uses PlannerInterface instead of direct post_flow()
- Gate: No direct create_flow calls from transport layer
"""

from __future__ import annotations

import pytest

from antigona.intent_router import IntentDecision
from antigona.planner.interface import FlowEnginePlanner, PlannerInterface, PlanResult


class TestFlowEnginePlanner:
    """Tests for the FlowEnginePlanner adapter."""

    @pytest.mark.asyncio
    async def test_plan_file_write_intent(self) -> None:
        """Planner resolves a file_write intent correctly."""
        planner = FlowEnginePlanner()
        decision = IntentDecision(
            intent="task.file_write",
            confidence=0.95,
            response_mode="task_preview",
            entities={"path": "test/hello.txt", "content": "Hello"},
            reason_code="deterministic.file_write",
        )
        result = await planner.plan(decision)
        assert result.success
        assert result.tool_name == "workspace.write_text"
        assert result.path == "test/hello.txt"
        assert result.content == "Hello"
        assert len(result.steps) == 1
        assert result.steps[0]["action"] == "create_flow"

    @pytest.mark.asyncio
    async def test_plan_shell_intent(self) -> None:
        """Planner resolves a shell intent correctly."""
        planner = FlowEnginePlanner()
        decision = IntentDecision(
            intent="task.shell",
            confidence=0.9,
            response_mode="task_preview",
            entities={"goal": "ls -la /tmp"},
            reason_code="deterministic.shell",
        )
        result = await planner.plan(decision, context={"text": "shell:ls -la /tmp"})
        assert result.success
        assert result.tool_name == "sandbox.shell"
        assert result.command == ["ls", "-la", "/tmp"]

    @pytest.mark.asyncio
    async def test_plan_with_context_text(self) -> None:
        """Planner uses context text when entities don't provide goal."""
        planner = FlowEnginePlanner()
        decision = IntentDecision(
            intent="task.file_write",
            confidence=0.9,
            response_mode="task_preview",
            entities={},
            reason_code="deterministic.generic",
        )
        result = await planner.plan(decision, context={"text": "create file foo.txt"})
        assert result.success
        assert result.goal == "create file foo.txt"

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_gateway_down(self) -> None:
        """Execute returns an error (not raises) when Gateway is unreachable."""
        planner = FlowEnginePlanner(gateway_url="http://127.0.0.1:1")
        plan = PlanResult(goal="test", tool_name="workspace.write_text")
        result = await planner.execute(plan)
        assert not result.success
        assert result.error is not None

    def test_plan_result_properties(self) -> None:
        """PlanResult.success property works correctly."""
        ok = PlanResult(goal="test")
        assert ok.success
        assert ok.error is None

        err = PlanResult(goal="test", error="something went wrong")
        assert not err.success
        assert err.error == "something went wrong"

    def test_planner_interface_is_abstract(self) -> None:
        """PlannerInterface cannot be instantiated directly."""
        with pytest.raises(TypeError):
            PlannerInterface()  # type: ignore[abstract]


class TestDay22Gate:
    """Gate: No direct create_flow from transport layer.

    This test scans the source to ensure the transport layer
    does not call the Gateway create_flow endpoint directly.
    """

    TRANSPORT_FILES = [
        "src/antigona/channels/telegram/bot.py",
        "src/antigona/transport/telegram.py",
    ]

    def test_transport_does_not_call_post_flow_directly_in_handle_decision(self) -> None:
        """The transport's handler uses planner, not direct post_flow."""
        import ast

        with open("src/antigona/channels/telegram/bot.py") as f:
            tree = ast.parse(f.read())

        # Find _handle_decision method and check it doesn't call self.post_flow()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_decision":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Attribute):
                            if (
                                isinstance(child.func.value, ast.Attribute)
                                and child.func.value.attr == "post_flow"
                                and child.func.attr in ("execute",)
                            ):
                                pass  # planner.execute is fine
                            elif (
                                isinstance(child.func.value, ast.Name)
                                and child.func.value.id == "self"
                                and child.func.attr == "post_flow"
                            ):
                                pytest.fail(
                                    "_handle_decision must not call self.post_flow directly. "
                                    "Use self.planner.execute() instead."
                                )

