from __future__ import annotations

import re
from typing import Any, Mapping

from ..contracts import RiskLevel, ToolContext, ToolResult, ToolSpec, ToolStatus
from ._paths import resolve_workspace_path


_MAX_MATCHES = 200


def _handler(arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
    root = resolve_workspace_path(str(arguments.get("path", ".")), context)
    pattern = re.compile(str(arguments["pattern"]))
    glob = str(arguments.get("glob", "**/*"))
    matches: list[str] = []

    for path in root.glob(glob):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                relative = path.relative_to(context.normalized_workspace())
                matches.append(f"{relative}:{line_no}:{line.strip()}")
                if len(matches) >= _MAX_MATCHES:
                    break
        if len(matches) >= _MAX_MATCHES:
            break

    return ToolResult(
        call_id="",
        tool_name="search_files",
        status=ToolStatus.SUCCESS,
        summary=f"Found {len(matches)} matching lines.",
        stdout="\n".join(matches),
        truncated=len(matches) >= _MAX_MATCHES,
        data={"matches": len(matches)},
    )


def spec() -> ToolSpec:
    return ToolSpec(
        name="search_files",
        description=(
            "Search text files in the workspace with a regular expression. Use this instead of terminal/grep. "
            "Narrow the path or glob before broad searches."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=_handler,
        toolset="filesystem",
        capabilities=frozenset({"search", "inspect"}),
        risk_level=RiskLevel.READ_ONLY,
        side_effects=False,
        idempotent=True,
        timeout_seconds=20,
    )
