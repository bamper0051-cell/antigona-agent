from __future__ import annotations

from pathlib import Path

from ..contracts import ToolContext


def resolve_workspace_path(raw: str, context: ToolContext) -> Path:
    workspace = context.normalized_workspace()
    candidate = (workspace / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"Path escapes workspace: {raw}") from exc
    return candidate
