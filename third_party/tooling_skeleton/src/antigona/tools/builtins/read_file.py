from __future__ import annotations

from typing import Any, Mapping

from ..contracts import RiskLevel, ToolContext, ToolResult, ToolSpec, ToolStatus
from ._paths import resolve_workspace_path


_MAX_CHARS = 100_000


def _handler(arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
    path = resolve_workspace_path(str(arguments["path"]), context)
    text = path.read_text(encoding="utf-8")
    truncated = len(text) > _MAX_CHARS
    visible = text[:_MAX_CHARS]
    return ToolResult(
        call_id="",
        tool_name="read_file",
        status=ToolStatus.SUCCESS,
        summary=f"Read {path.relative_to(context.normalized_workspace())}",
        stdout=visible,
        truncated=truncated,
        artifacts=(str(path),),
        data={"characters": len(text)},
    )


def spec() -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file inside the active workspace. Use this instead of terminal/cat. "
            "Never use it for paths outside the workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_handler,
        toolset="filesystem",
        capabilities=frozenset({"read", "inspect"}),
        risk_level=RiskLevel.READ_ONLY,
        side_effects=False,
        idempotent=True,
        timeout_seconds=10,
    )
