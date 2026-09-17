from __future__ import annotations

import shutil
from typing import Any, Mapping

from ..contracts import RiskLevel, ToolContext, ToolResult, ToolSpec, ToolStatus
from ._paths import resolve_workspace_path
from ._process import run_process


_MAX_OUTPUT = 100_000


async def _handler(arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
    command = [str(part) for part in arguments["command"]]
    if not command:
        raise ValueError("command must not be empty")
    if shutil.which(command[0]) is None:
        return ToolResult(
            call_id="",
            tool_name="terminal",
            status=ToolStatus.FAILED,
            summary=f"Command not found: {command[0]}",
            error_type="COMMAND_NOT_FOUND",
            retryable=False,
        )
    cwd = resolve_workspace_path(str(arguments.get("cwd", ".")), context)
    timeout = float(arguments.get("timeout_seconds", 60))
    try:
        exit_code, stdout, stderr = await run_process(command, cwd, timeout)
    except TimeoutError:
        return ToolResult(
            call_id="",
            tool_name="terminal",
            status=ToolStatus.TIMEOUT,
            summary=f"Command exceeded {timeout:g}s timeout.",
            error_type="TIMEOUT",
            retryable=True,
            data={"command": command},
        )
    truncated = len(stdout) + len(stderr) > _MAX_OUTPUT
    stdout = stdout[:_MAX_OUTPUT]
    stderr = stderr[:_MAX_OUTPUT]
    status = ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.FAILED
    return ToolResult(
        call_id="",
        tool_name="terminal",
        status=status,
        summary=f"Command exited with code {exit_code}.",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_type=None if exit_code == 0 else "NON_ZERO_EXIT",
        retryable=False,
        truncated=truncated,
    )


def spec() -> ToolSpec:
    return ToolSpec(
        name="terminal",
        description=(
            "Run an argv-style command inside the active workspace without a shell. Use for build, lint, "
            "tests and diagnostics only when no specialized tool exists. Every call needs a new hypothesis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "array"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=_handler,
        toolset="terminal",
        capabilities=frozenset({"execute", "diagnose"}),
        risk_level=RiskLevel.SAFE_EXECUTION,
        side_effects=True,
        idempotent=False,
        timeout_seconds=300,
    )
