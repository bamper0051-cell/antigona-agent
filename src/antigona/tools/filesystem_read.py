"""Read-only filesystem tool — list/read/search with root boundary enforcement.

This tool provides safe, read-only access to the filesystem.
It enforces a workspace boundary (authoritative workspace_dir, never the
filesystem root or the real home directory)
and will not allow access to paths outside that boundary, even via symlinks.
"""

from __future__ import annotations

import os
from pathlib import Path

from antigona.core.paths import home_dir
from antigona.path_boundary import has_unsafe_relative_path_syntax
from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def _resolve_safe(base: Path, target: str) -> Path:
    """Resolve a target path relative to base, ensuring it stays within base.

    Args:
        base: The root boundary path (must be absolute).
        target: A relative path string.

    Returns:
        The resolved absolute path.

    Raises:
        ValueError: If the resolved path is outside the boundary or has unsafe syntax.
    """
    if not isinstance(base, Path) or not base.is_absolute():
        raise ValueError(f"Base path must be absolute, got: {base}")

    if not isinstance(target, str):
        raise ValueError(f"Target path must be a string, got: {type(target).__name__}")

    clean = target.strip()
    if clean in ("", "."):
        return base.resolve()

    if has_unsafe_relative_path_syntax(target):
        raise ValueError(
            f"Path '{target}' contains unsafe traversal syntax: outside boundary '{base}'"
        )

    raw = Path(target)
    if raw.is_absolute():
        raise ValueError(f"Path '{target}' is absolute: outside boundary '{base}'")

    base_resolved = Path(os.path.realpath(base))
    resolved = Path(os.path.realpath(base_resolved / raw))

    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(
            f"Path '{target}' resolves to '{resolved}' which is outside boundary '{base_resolved}'"
        ) from exc

    # Component-level containment check: verify no symlink component escapes base
    current = base_resolved
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            current_resolved = Path(os.path.realpath(current))
            try:
                current_resolved.relative_to(base_resolved)
            except ValueError as exc:
                raise ValueError(
                    f"Path '{target}' component '{part}' is a symlink pointing outside boundary '{base_resolved}'"
                ) from exc

    return resolved


class FilesystemReadTool(Tool):
    """Read-only filesystem tool.

    Provides:
    - list: List directory contents.
    - read: Read file contents.
    - search: Search files by name pattern.

    Enforces a workspace boundary that cannot be escaped.
    """

    def __init__(self, root_boundary: str | None = None) -> None:
        if root_boundary:
            resolved_root = Path(root_boundary).resolve()
        elif os.environ.get("ANTIGONA_WORKSPACE"):
            resolved_root = Path(os.environ["ANTIGONA_WORKSPACE"]).resolve()
        else:
            from antigona.core import paths
            try:
                resolved_root = paths.workspace_dir().resolve()
            except Exception as exc:
                raise ValueError(f"Unable to establish authoritative workspace root: {exc}") from exc

        # Deny the filesystem root and the *real* home directory. The home
        # entry used to be the literal "/root"; off-host the actual home was
        # NOT rejected. Derive it from the single home resolver (ADR-007).
        forbidden_home = home_dir().resolve()
        if not resolved_root.is_absolute() or resolved_root == Path("/") or resolved_root == forbidden_home:
            raise ValueError(f"Invalid workspace root boundary: {resolved_root}")

        self._root = resolved_root
        self._root_str = str(self._root)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="filesystem.read",
            category=ToolCategory.FILESYSTEM_READ,
            description="Read-only filesystem: list, read, search within root boundary",
            risk_level=RiskLevel.LOW,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "search"],
                        "description": "Action to perform",
                    },
                    "path": {
                        "type": "string",
                        "description": "Target path (relative to root boundary)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "File glob pattern for search action",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results for list/search",
                        "default": 50,
                    },
                },
                "required": ["action"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "description": "List of file/dir entries"},
                    "content": {"type": "string", "description": "File content"},
                    "total": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                },
            },
            allowed_targets=[str(self._root)],
            timeout_seconds=10,
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors: list[str] = []
        action = inp.params.get("action", "")
        if action not in ("list", "read", "search"):
            errors.append(f"Invalid action: '{action}'. Must be one of: list, read, search")

        path = inp.params.get("path", "")
        if action == "read":
            if not path:
                errors.append("path is required for action 'read'")
            elif has_unsafe_relative_path_syntax(path):
                errors.append(f"path '{path}' contains unsafe traversal syntax or violates boundary")
        elif action == "list":
            if path and path not in (".", ""):
                if has_unsafe_relative_path_syntax(path):
                    errors.append(f"path '{path}' contains unsafe traversal syntax or violates boundary")
        elif action == "search":
            pattern = inp.params.get("pattern", "")
            if not pattern:
                errors.append("search pattern is required")
            elif pattern.startswith("/") or has_unsafe_relative_path_syntax(pattern):
                errors.append(f"search pattern '{pattern}' contains unsafe traversal syntax")

        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        action = inp.params.get("action", "")
        target_path = inp.params.get("path", "")
        pattern = inp.params.get("pattern", "")
        max_results = int(inp.params.get("max_results", 50))

        if inp.dry_run:
            return ToolOutput(success=True, data={"action": action, "path": target_path, "dry_run": True})

        try:
            if action == "list":
                return self._list(target_path, max_results)
            elif action == "read":
                return self._read(target_path)
            elif action == "search":
                return self._search(pattern, max_results)
            else:
                return ToolOutput(success=False, error=f"Unknown action: {action}")
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        except FileNotFoundError as exc:
            return ToolOutput(success=False, error=f"Not found: {exc}")
        except PermissionError as exc:
            return ToolOutput(success=False, error=f"Permission denied: {exc}")
        except OSError as exc:
            return ToolOutput(success=False, error=f"OS error: {exc}")

    def _list(self, path: str, max_results: int) -> ToolOutput:
        resolved = _resolve_safe(self._root, path)
        if not resolved.is_dir():
            return ToolOutput(success=False, error=f"Not a directory: {path}")

        items: list[dict[str, object]] = []
        root_resolved = self._root.resolve()
        for entry in sorted(resolved.iterdir()):
            try:
                entry_resolved = entry.resolve()
                if not entry_resolved.is_relative_to(root_resolved):
                    continue  # skip entries that resolve outside root boundary
                stat_info = entry.stat()
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat_info.st_size,
                    "modified": stat_info.st_mtime,
                })
            except OSError:
                items.append({
                    "name": entry.name,
                    "type": "unknown",
                })
            if len(items) >= max_results:
                break

        truncated = len(items) >= max_results and len(list(resolved.iterdir())) > max_results
        return ToolOutput(success=True, data={
            "items": items,
            "total": len(items),
            "truncated": truncated,
            "path": str(resolved),
        })

    def _read(self, path: str) -> ToolOutput:
        if not path:
            return ToolOutput(success=False, error="path is required for action 'read'")
        resolved = _resolve_safe(self._root, path)
        if not resolved.is_file():
            return ToolOutput(success=False, error=f"Not a file: {path}")
        if resolved.stat().st_size > 10 * 1024 * 1024:
            return ToolOutput(success=False, error="File too large (>10MB)")

        content = resolved.read_text(encoding="utf-8", errors="replace")
        return ToolOutput(success=True, data={
            "content": content,
            "path": str(resolved),
            "size": len(content),
        })

    def _search(self, pattern: str, max_results: int) -> ToolOutput:
        """Search for files by glob pattern within the root boundary."""
        if not pattern:
            return ToolOutput(success=False, error="search pattern is required")
        if pattern.startswith("/") or has_unsafe_relative_path_syntax(pattern):
            return ToolOutput(success=False, error=f"unsafe search pattern: '{pattern}'")

        matches: list[dict[str, object]] = []
        root_resolved = self._root.resolve()
        # Use rglob to search recursively, but verify each result is within boundary
        for entry in sorted(self._root.rglob(pattern)):
            try:
                resolved = entry.resolve()
                if not resolved.is_relative_to(root_resolved):
                    continue  # skip symlinks that escape boundary
                stat_info = entry.stat()
                matches.append({
                    "path": str(entry.relative_to(self._root)),
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat_info.st_size,
                })
                if len(matches) >= max_results:
                    break
            except (OSError, ValueError):
                continue

        truncated = len(matches) >= max_results
        return ToolOutput(success=True, data={
            "items": matches,
            "total": len(matches),
            "truncated": truncated,
            "pattern": pattern,
        })


# Re-export for backwards compatibility
# _resolve_safe is defined at the top of this file
__all__ = [
    "FilesystemReadTool",
    "_resolve_safe",
]
