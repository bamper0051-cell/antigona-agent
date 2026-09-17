"""Tool contracts — schemas for all tool inputs, outputs, and specifications.

Every tool in the system implements the Tool interface. Tools are registered
in the ToolRegistry and executed through the Policy → Executor pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RiskLevel(StrEnum):
    """Risk level for tool execution, used by the PolicyEngine."""
    SAFE = "SAFE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolCategory(StrEnum):
    """Category of tool — determines which executor handles it."""
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    SHELL = "shell"
    NETWORK = "network"
    CODE = "code"
    MOCK = "mock"


class ToolStatus(StrEnum):
    """Lifecycle status of a registered tool."""
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    DEPRECATED = "DEPRECATED"


@dataclass
class ToolSpec:
    """Full specification of a tool.

    Attributes:
        name: Unique tool identifier (e.g. "filesystem.read").
        category: Tool category for routing.
        description: Human-readable description.
        version: Semver string.
        risk_level: Default risk level when used without explicit policy.
        input_schema: JSON Schema for the tool's input parameters.
        output_schema: JSON Schema for the tool's output.
        requires_approval: Whether HITL approval is needed by default.
        allowed_targets: List of allowed target patterns (paths, commands, etc.).
        forbidden_targets: List of forbidden target patterns.
        timeout_seconds: Default timeout for this tool.
        status: Current lifecycle status.
    """

    name: str
    category: ToolCategory
    description: str = ""
    version: str = "0.1.0"
    risk_level: RiskLevel = RiskLevel.MEDIUM
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    allowed_targets: list[str] = field(default_factory=list)
    forbidden_targets: list[str] = field(default_factory=list)
    timeout_seconds: int = 30
    status: ToolStatus = ToolStatus.ACTIVE


@dataclass
class ToolInput:
    """Base input for any tool execution.

    Attributes:
        tool_name: The name of the tool to execute.
        params: Tool-specific parameters.
        context: Execution context (user_id, chat_id, etc.).
        timeout_seconds: Override timeout for this execution.
        dry_run: If True, validate but don't execute.
        correlation_id: Correlation ID for this execution.
    """

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    dry_run: bool = False
    correlation_id: str = ""


@dataclass
class ToolOutput:
    """Standard output from any tool execution.

    Attributes:
        success: Whether execution succeeded.
        data: The result data.
        error: Error message if execution failed.
        artifacts: List of artifact references produced.
        duration_ms: Execution duration in milliseconds.
        requires_approval: Whether this output still requires approval.
        verification_needed: Whether post-execution verification is needed.
    """

    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    requires_approval: bool = False
    verification_needed: bool = False


class Tool(ABC):
    """Abstract base for all tools.

    Every tool must:
    - Declare its spec (name, category, risk level, schemas)
    - Implement validate() to check inputs before execution
    - Implement execute() to perform the actual work
    """

    @property
    def name(self) -> str:
        """Compatibility name used by mixed descriptor/contract registries."""
        return self.spec.name

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """Return the tool's specification."""
        ...

    @abstractmethod
    def validate(self, inp: ToolInput) -> list[str]:
        """Validate tool input against the spec.

        Returns a list of validation error messages.
        Empty list means the input is valid.
        """
        ...

    @abstractmethod
    async def execute(self, inp: ToolInput) -> ToolOutput:
        """Execute the tool with the given input.

        Args:
            inp: The validated tool input.

        Returns:
            ToolOutput with success/failure and result data.
        """
        ...


class MockTool(Tool):
    """Mock tool for testing the tool framework.

    Always succeeds unless configured to fail.
    """

    def __init__(
        self,
        name: str = "mock.test",
        category: ToolCategory = ToolCategory.MOCK,
        risk_level: RiskLevel = RiskLevel.LOW,
        fail_on_input: str | None = None,
        fixed_output: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._category = category
        self._risk_level = risk_level
        self._fail_on_input = fail_on_input
        self._fixed_output = fixed_output or {"result": "mock_ok"}
        self._execution_count = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            category=self._category,
            description="Mock tool for testing",
            risk_level=self._risk_level,
            input_schema={
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                },
            },
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors: list[str] = []
        if self._fail_on_input and inp.params.get("input") == self._fail_on_input:
            errors.append(f"Input '{self._fail_on_input}' is configured to fail validation")
        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        self._execution_count += 1
        errors = self.validate(inp)
        if errors:
            return ToolOutput(success=False, error="; ".join(errors))

        if inp.dry_run:
            return ToolOutput(success=True, data={"dry_run": True, "spec": self._name})

        return ToolOutput(success=True, data=self._fixed_output)

    @property
    def execution_count(self) -> int:
        return self._execution_count


class FailingTool(Tool):
    """Tool that always fails — for testing error handling."""

    def __init__(self, name: str = "mock.fail", error_message: str = "intentional failure") -> None:
        self._name = name
        self._error_message = error_message

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            category=ToolCategory.MOCK,
            description="Always-failing mock tool",
            risk_level=RiskLevel.LOW,
        )

    def validate(self, inp: ToolInput) -> list[str]:
        return []

    async def execute(self, inp: ToolInput) -> ToolOutput:
        return ToolOutput(success=False, error=self._error_message)
