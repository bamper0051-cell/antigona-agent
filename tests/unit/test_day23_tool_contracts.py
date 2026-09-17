"""Day 23 tests: Tool contracts — Schema, registry, lifecycle, validation.

Verifies:
- ToolSpec, ToolInput, ToolOutput contract shapes
- MockTool and FailingTool behavior
- ToolRegistry registration, lookup, lifecycle
- Validation gates
"""

from __future__ import annotations

import pytest

from antigona.tools.contracts import (
    FailingTool,
    MockTool,
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolSpec,
    ToolStatus,
)
from antigona.tools.registry import ToolNotFoundError, ToolRegistrationError, ToolRegistry


class TestToolSpec:
    """ToolSpec contract tests."""

    def test_default_values(self) -> None:
        spec = ToolSpec(name="test.tool", category=ToolCategory.MOCK)
        assert spec.name == "test.tool"
        assert spec.category == ToolCategory.MOCK
        assert spec.risk_level == RiskLevel.MEDIUM
        assert spec.version == "0.1.0"
        assert spec.status == ToolStatus.ACTIVE
        assert not spec.requires_approval

    def test_custom_values(self) -> None:
        spec = ToolSpec(
            name="filesystem.read",
            category=ToolCategory.FILESYSTEM_READ,
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            version="1.0.0",
            status=ToolStatus.ACTIVE,
            timeout_seconds=10,
        )
        assert spec.name == "filesystem.read"
        assert spec.risk_level == RiskLevel.LOW
        assert spec.timeout_seconds == 10


class TestMockTool:
    """MockTool behavior tests."""

    @pytest.mark.asyncio
    async def test_mock_tool_success(self) -> None:
        tool = MockTool()
        inp = ToolInput(tool_name="mock.test", params={"input": "hello"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data == {"result": "mock_ok"}
        assert tool.execution_count == 1

    @pytest.mark.asyncio
    async def test_mock_tool_validation_failure(self) -> None:
        tool = MockTool(fail_on_input="bad")
        inp = ToolInput(tool_name="mock.test", params={"input": "bad"})
        out = await tool.execute(inp)
        assert not out.success
        assert "bad" in (out.error or "")

    @pytest.mark.asyncio
    async def test_mock_tool_dry_run(self) -> None:
        tool = MockTool()
        inp = ToolInput(tool_name="mock.test", params={"input": "test"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("dry_run") is True

    @pytest.mark.asyncio
    async def test_mock_tool_custom_output(self) -> None:
        tool = MockTool(fixed_output={"custom": "data"})
        inp = ToolInput(tool_name="mock.test", params={"input": "test"})
        out = await tool.execute(inp)
        assert out.data == {"custom": "data"}

    def test_mock_tool_spec(self) -> None:
        tool = MockTool(name="custom.mock", risk_level=RiskLevel.HIGH)
        spec = tool.spec
        assert spec.name == "custom.mock"
        assert spec.risk_level == RiskLevel.HIGH

    def test_execution_count(self) -> None:
        tool = MockTool()
        assert tool.execution_count == 0


class TestFailingTool:
    """FailingTool behavior tests."""

    @pytest.mark.asyncio
    async def test_failing_tool_always_fails(self) -> None:
        tool = FailingTool()
        inp = ToolInput(tool_name="mock.fail", params={})
        out = await tool.execute(inp)
        assert not out.success
        assert out.error == "intentional failure"

    @pytest.mark.asyncio
    async def test_failing_tool_custom_message(self) -> None:
        tool = FailingTool(error_message="custom error")
        inp = ToolInput(tool_name="mock.fail", params={})
        out = await tool.execute(inp)
        assert not out.success
        assert out.error == "custom error"


class TestToolRegistry:
    """ToolRegistry lifecycle tests."""

    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = MockTool(name="test.register")
        registry.register(tool)
        assert "test.register" in registry
        assert registry.count == 1
        retrieved = registry.get("test.register")
        assert retrieved is tool

    def test_register_duplicate_raises(self) -> None:
        registry = ToolRegistry()
        registry.register(MockTool(name="dup"))
        with pytest.raises(ToolRegistrationError, match="already registered"):
            registry.register(MockTool(name="dup"))

    def test_get_missing_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError, match="not registered"):
            registry.get("nonexistent")

    def test_unregister(self) -> None:
        registry = ToolRegistry()
        registry.register(MockTool(name="remove_me"))
        assert registry.count == 1
        registry.unregister("remove_me")
        assert registry.count == 0
        with pytest.raises(ToolNotFoundError):
            registry.get("remove_me")

    def test_unregister_missing_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            registry.unregister("nonexistent")

    def test_get_spec(self) -> None:
        registry = ToolRegistry()
        tool = MockTool(name="spec_test")
        registry.register(tool)
        spec = registry.get_spec("spec_test")
        assert spec.name == "spec_test"

    def test_list_tools_all(self) -> None:
        registry = ToolRegistry()
        registry.register(MockTool(name="tool.a"))
        registry.register(MockTool(name="tool.b"))
        assert len(registry.list_tools()) == 2

    def test_list_tools_by_category(self) -> None:
        registry = ToolRegistry()
        registry.register(MockTool(name="tool.mock", category=ToolCategory.MOCK))
        registry.register(FailingTool(name="tool.fail"))
        mock_tools = registry.list_tools(category="mock")
        assert len(mock_tools) == 2  # Both MockTool and FailingTool have MOCK category
        names = {s.name for s in mock_tools}
        assert "tool.mock" in names
        assert "tool.fail" in names

    def test_contains(self) -> None:
        registry = ToolRegistry()
        registry.register(MockTool(name="present"))
        assert "present" in registry
        assert "missing" not in registry

    def test_multiple_tools(self) -> None:
        registry = ToolRegistry()
        for i in range(10):
            registry.register(MockTool(name=f"tool.{i}"))
        assert registry.count == 10
        specs = registry.list_tools()
        assert len(specs) == 10
        names = [s.name for s in specs]
        assert "tool.0" in names
        assert "tool.9" in names


class TestToolInterface:
    """Abstract interface contract tests."""

    def test_tool_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            Tool()  # type: ignore[abstract]

    def test_tool_implements_all_abstract(self) -> None:
        """MockTool and FailingTool fully implement the Tool ABC."""
        mock = MockTool()
        assert hasattr(mock, "spec")
        assert hasattr(mock, "validate")
        assert hasattr(mock, "execute")
        fail = FailingTool()
        assert hasattr(fail, "spec")
        assert hasattr(fail, "validate")
        assert hasattr(fail, "execute")
