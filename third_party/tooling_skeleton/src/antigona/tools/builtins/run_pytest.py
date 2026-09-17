from __future__ import annotations

import re
import shutil
from typing import Any, Mapping

from ..contracts import RiskLevel, ToolContext, ToolResult, ToolSpec, ToolStatus
from ._paths import resolve_workspace_path
from ._process import run_process


_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed|(?P<failed>\d+) failed|(?P<errors>\d+) errors?|(?P<skipped>\d+) skipped"
)


def _python_for_workspace(context: ToolContext) -> str:
    workspace = context.normalized_workspace()
    venv_python = workspace / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return shutil.which("python") or "python"


async def _handler(arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
    paths = [str(item) for item in arguments.get("paths", [])]
    extra_args = [str(item) for item in arguments.get("extra_args", [])]
    timeout = float(arguments.get("timeout_seconds", 120))
    cwd = resolve_workspace_path(str(arguments.get("cwd", ".")), context)
    command = [_python_for_workspace(context), "-m", "pytest", *paths, "--tb=short", *extra_args]

    try:
        exit_code, stdout, stderr = await run_process(command, cwd, timeout)
    except TimeoutError:
        return ToolResult(
            call_id="",
            tool_name="run_pytest",
            status=ToolStatus.TIMEOUT,
            summary=f"pytest exceeded {timeout:g}s timeout; reduce scope or inspect slow tests.",
            error_type="TIMEOUT",
            retryable=True,
            data={"command": command},
        )
    combined = f"{stdout}\n{stderr}"
    counters = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for match in _SUMMARY_RE.finditer(combined):
        for key, value in match.groupdict().items():
            if value is not None:
                counters[key] = max(counters[key], int(value))

    status = ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.FAILED
    return ToolResult(
        call_id="",
        tool_name="run_pytest",
        status=status,
        summary=(
            f"pytest exit={exit_code}; passed={counters['passed']}, failed={counters['failed']}, "
            f"errors={counters['errors']}, skipped={counters['skipped']}"
        ),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_type=None if exit_code == 0 else "PYTEST_FAILED",
        retryable=False,
        data={"command": command, **counters},
    )


def spec() -> ToolSpec:
    return ToolSpec(
        name="run_pytest",
        description=(
            "Run pytest through the project .venv when available and return structured counters. "
            "Start with targeted paths; run the full suite only after related tests pass."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "paths": {"type": "array"},
                "extra_args": {"type": "array"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_handler,
        toolset="testing",
        capabilities=frozenset({"test", "verify"}),
        risk_level=RiskLevel.SAFE_EXECUTION,
        side_effects=True,
        idempotent=True,
        timeout_seconds=600,
    )
