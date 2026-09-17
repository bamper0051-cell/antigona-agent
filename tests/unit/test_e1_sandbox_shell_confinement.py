"""E-1: dialogue ``sandbox.shell`` must NOT execute on the host shell.

Regression guard for the defect where ``sandbox.shell`` (issued by the
dialogue path in ``core/brain.py``) was dispatched to the registry's
``run_shell`` handler, which runs ``asyncio.create_subprocess_shell`` on the
HOST. The fix routes it through ``antigona.shell.DockerShellTool``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from antigona.contracts import ToolResult
from antigona.engine.unified_executor import (
    ToolExecutionRequest,
    UnifiedToolExecutionLayer,
)


class _AllowPolicy:
    async def check(self, **_kwargs: Any) -> dict[str, Any]:
        return {"allowed": True}


class _RecordingRegistry:
    """Registry that fails the test if the host shell is ever dispatched."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, tool_name: str, **kwargs: Any) -> str:
        self.calls.append(tool_name)
        return json.dumps({"success": True, "output": "HOST"})


class _FakeDockerShell:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def execute(self, arguments: Any) -> ToolResult:
        self.commands.append(tuple(arguments.command))
        return ToolResult(True, "completed", {"output": "HOME=/app"})


def _executor(registry: _RecordingRegistry, sandbox: _FakeDockerShell) -> UnifiedToolExecutionLayer:
    return UnifiedToolExecutionLayer(
        policy_engine=_AllowPolicy(),
        registry=registry,
        sandbox_shell_tool=sandbox,
    )


@pytest.mark.asyncio
async def test_dialogue_sandbox_shell_never_reaches_host_run_shell() -> None:
    registry = _RecordingRegistry()
    sandbox = _FakeDockerShell()
    executor = _executor(registry, sandbox)

    raw = await executor.execute(
        ToolExecutionRequest(
            tool_name="sandbox.shell",
            params={"command": "echo HOME=$HOME"},
            requester="dialogue",
            correlation_id=f"e1-corr-{uuid.uuid4().hex}",
            turn_id=f"e1-turn-{uuid.uuid4().hex}",
        )
    )

    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["sandboxed"] is True
    assert payload["output"] == "HOME=/app"
    # The host shell handler must never be dispatched for sandbox.shell.
    assert "run_shell" not in registry.calls
    assert registry.calls == []
    assert sandbox.commands == [("echo HOME=$HOME",)]


@pytest.mark.asyncio
async def test_dialogue_sandbox_shell_with_metacharacters_stays_sandboxed() -> None:
    registry = _RecordingRegistry()
    sandbox = _FakeDockerShell()
    executor = _executor(registry, sandbox)

    raw = await executor.execute(
        ToolExecutionRequest(
            tool_name="sandbox.shell",
            params={"command": "pwd; cat /opt/antigona-home/.antigona/.env"},
            requester="dialogue",
            correlation_id=f"e1-corr-{uuid.uuid4().hex}",
            turn_id=f"e1-turn-{uuid.uuid4().hex}",
        )
    )

    assert json.loads(raw)["sandboxed"] is True
    assert registry.calls == []
    assert sandbox.commands == [("pwd; cat /opt/antigona-home/.antigona/.env",)]


@pytest.mark.asyncio
async def test_model_run_shell_still_requires_exact_owner_approval() -> None:
    """The PIN/approval-gated host path is unchanged (fail-closed)."""
    registry = _RecordingRegistry()
    sandbox = _FakeDockerShell()
    executor = _executor(registry, sandbox)

    raw = await executor.execute(
        ToolExecutionRequest(
            tool_name="run_shell",
            params={"command": "pwd"},
            requester="llm",
            correlation_id=f"e1-corr-{uuid.uuid4().hex}",
            turn_id=f"e1-turn-{uuid.uuid4().hex}",
        )
    )

    assert "exact owner approval is required" in json.loads(raw)["error"]
    assert registry.calls == []
    assert sandbox.commands == []
