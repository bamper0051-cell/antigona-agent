"""Write-enabled filesystem tool — create, edit, patch with diff preview.

This tool provides safe, write-controlled filesystem operations.
Every write action requires explicit approval before execution and
returns a diff preview showing what will change.

Enforces a root boundary (defaults to the authoritative ``workspace_dir()``)
that cannot be escaped, even via symlinks.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)


def _resolve_safe(base: Path, target: str) -> Path:
    """Resolve a target path relative to base, ensuring it stays within base."""
    from antigona.worker.tools.common import ToolError, WorkspaceGuard

    if not base.is_absolute():
        raise ValueError(f"Base path must be absolute, got: {base}")

    clean = target.lstrip("/") if target.startswith("/") else target
    try:
        return WorkspaceGuard(base).resolve(clean)
    except (ToolError, ValueError) as exc:
        raise ValueError(
            f"Path '{target}' violates workspace boundary '{base}': {exc}"
        ) from exc


def _generate_diff(original: str, updated: str, path_hint: str) -> str:
    """Generate a unified diff between original and updated content.

    Args:
        original: The original file content (empty string for new files).
        updated: The new file content.
        path_hint: Display path for the diff header.

    Returns:
        A unified diff string.
    """
    original_lines = original.splitlines(keepends=True)
    updated_lines = updated.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        updated_lines,
        fromfile=f"a/{path_hint}",
        tofile=f"b/{path_hint}",
        lineterm="\n",
    )
    return "".join(diff)


def _check_create_overwrite(path: Path) -> str | None:
    """Check if a path can be created. Returns None if ok, error string if not.

    Args:
        path: The target file path.

    Returns:
        None if the file can be created, or an error message string.
    """
    if path.exists():
        return f"File already exists: {path}"
    if path.is_symlink():
        return f"Symlink already exists at path: {path}"
    # Check parent exists
    if not path.parent.exists():
        return f"Parent directory does not exist: {path.parent}"
    return None


def _check_edit_target(path: Path) -> str | None:
    """Check if a path can be edited. Returns None if ok, error string if not.

    Args:
        path: The target file path.

    Returns:
        None if the file can be edited, or an error message string.
    """
    if not path.exists():
        return f"File does not exist: {path}"
    if not path.is_file():
        return f"Not a regular file: {path}"
    if not os.access(path, os.R_OK):
        return f"File is not readable: {path}"
    return None


class FilesystemWriteTool(Tool):
    """Write-enabled filesystem tool.

    Provides:
    - create_file: Create a new file with content.
    - edit_file: Replace content of an existing file.
    - patch: Find and replace text within a file.

    Every action:
    - Enforces a root boundary (default: authoritative ``workspace_dir()`` / ``ANTIGONA_WORKSPACE``).
    - Returns a diff preview of changes.
    - Requires explicit approval before writing (set via requires_approval in execute).
    - Is idempotent via dry_run (returns preview without writing).

    Risk level: MEDIUM (create_file), HIGH (edit_file, patch).
    """

    def __init__(self, root_boundary: str | None = None) -> None:
        from antigona.core import paths
        default_root = os.environ.get("ANTIGONA_WORKSPACE", str(paths.workspace_dir()))
        self._root = Path(root_boundary or default_root).resolve()
        self._root_str = str(self._root)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="filesystem.write",
            category=ToolCategory.FILESYSTEM_WRITE,
            description="Write to filesystem: create, edit, patch with diff preview, root boundary enforced",
            risk_level=RiskLevel.HIGH,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create_file", "edit_file", "patch"],
                        "description": "Action to perform",
                    },
                    "path": {
                        "type": "string",
                        "description": "Target path (relative to root boundary)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content (for create_file, edit_file)",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Text to find (for patch action)",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text (for patch action)",
                    },
                },
                "required": ["action", "path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "path": {"type": "string"},
                    "diff": {"type": "string", "description": "Unified diff of changes"},
                    "written": {"type": "boolean"},
                    "requires_approval": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                    "preview": {"type": "string", "description": "Human-readable preview of changes"},
                },
            },
            allowed_targets=[str(self._root)],
            requires_approval=True,
            timeout_seconds=10,
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors: list[str] = []
        action = inp.params.get("action", "")
        valid_actions = ("create_file", "edit_file", "patch")
        if action not in valid_actions:
            errors.append(f"Invalid action: '{action}'. Must be one of: {', '.join(valid_actions)}")

        path = inp.params.get("path", "")
        if not path:
            errors.append("path is required")
        if path and path.strip().startswith("/"):
            errors.append("path must be relative to the root boundary, not absolute")

        if action == "create_file":
            if "content" not in inp.params:
                errors.append("content is required for create_file action")

        if action == "edit_file":
            if "content" not in inp.params:
                errors.append("content is required for edit_file action")

        if action == "patch":
            if "old_string" not in inp.params:
                errors.append("old_string is required for patch action")
            if "new_string" not in inp.params:
                errors.append("new_string is required for patch action")
            if not inp.params.get("old_string", ""):
                errors.append("old_string must not be empty")

        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        action = inp.params.get("action", "")
        target_path = inp.params.get("path", "")
        content = inp.params.get("content", "")
        old_string = inp.params.get("old_string", "")
        new_string = inp.params.get("new_string", "")

        try:
            resolved = _resolve_safe(self._root, target_path)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        if action == "create_file":
            return self._create_file(resolved, target_path, content, inp.dry_run)
        elif action == "edit_file":
            return self._edit_file(resolved, target_path, content, inp.dry_run)
        elif action == "patch":
            return self._patch_file(resolved, target_path, old_string, new_string, inp.dry_run)
        else:
            return ToolOutput(success=False, error=f"Unknown action: {action}")

    def _create_file(self, resolved: Path, display_path: str, content: str, dry_run: bool) -> ToolOutput:
        """Create a new file with the given content.

        Returns a diff preview showing the new content.
        Always sets requires_approval=True until explicitly approved.
        """
        # Check preconditions
        check = _check_create_overwrite(resolved)
        if check:
            return ToolOutput(success=False, error=check)

        # Generate diff preview (empty original → new content)
        diff = _generate_diff("", content, display_path)
        preview = f"CREATE {display_path}\n--- a/{display_path}\n+++ b/{display_path}\n{diff}"

        if dry_run:
            return ToolOutput(
                success=True,
                data={
                    "action": "create_file",
                    "path": display_path,
                    "diff": diff,
                    "written": False,
                    "dry_run": True,
                    "preview": preview,
                    "requires_approval": True,
                },
                requires_approval=True,
            )

        # Write the file
        try:
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Write failed: {exc}")

        return ToolOutput(
            success=True,
            data={
                "action": "create_file",
                "path": display_path,
                "diff": diff,
                "written": True,
                "preview": preview,
            },
            requires_approval=False,
            verification_needed=True,
        )

    def _edit_file(self, resolved: Path, display_path: str, content: str, dry_run: bool) -> ToolOutput:
        """Replace the full content of an existing file.

        Returns a diff preview of changes.
        Always sets requires_approval=True until explicitly approved.
        """
        # Check preconditions
        check = _check_edit_target(resolved)
        if check:
            return ToolOutput(success=False, error=check)

        # Read original content
        try:
            original = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Read failed: {exc}")

        if original == content:
            return ToolOutput(
                success=True,
                data={
                    "action": "edit_file",
                    "path": display_path,
                    "written": False,
                    "preview": "No changes — content is identical",
                    "diff": "",
                    "unchanged": True,
                },
            )

        # Generate diff preview
        diff = _generate_diff(original, content, display_path)
        preview = f"EDIT {display_path}\n{diff}"

        if dry_run:
            return ToolOutput(
                success=True,
                data={
                    "action": "edit_file",
                    "path": display_path,
                    "diff": diff,
                    "written": False,
                    "dry_run": True,
                    "preview": preview,
                    "requires_approval": True,
                },
                requires_approval=True,
            )

        # Write the file
        try:
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Write failed: {exc}")

        return ToolOutput(
            success=True,
            data={
                "action": "edit_file",
                "path": display_path,
                "diff": diff,
                "written": True,
                "preview": preview,
            },
            requires_approval=False,
            verification_needed=True,
        )

    def _patch_file(self, resolved: Path, display_path: str, old_string: str, new_string: str, dry_run: bool) -> ToolOutput:
        """Find and replace text within a file.

        Returns a diff preview of changes.
        Always sets requires_approval=True until explicitly approved.
        """
        # Check preconditions
        check = _check_edit_target(resolved)
        if check:
            return ToolOutput(success=False, error=check)

        # Read original content
        try:
            original = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Read failed: {exc}")

        if old_string not in original:
            return ToolOutput(
                success=False,
                error=f"old_string not found in '{display_path}'",
            )

        updated = original.replace(old_string, new_string, 1)
        if original == updated:
            return ToolOutput(
                success=True,
                data={
                    "action": "patch",
                    "path": display_path,
                    "written": False,
                    "preview": "No changes — replacement produced identical content",
                    "diff": "",
                    "unchanged": True,
                },
            )

        # Generate diff preview
        diff = _generate_diff(original, updated, display_path)
        preview = f"PATCH {display_path}\nReplace: {old_string!r} → {new_string!r}\n{diff}"

        if dry_run:
            return ToolOutput(
                success=True,
                data={
                    "action": "patch",
                    "path": display_path,
                    "diff": diff,
                    "written": False,
                    "dry_run": True,
                    "preview": preview,
                    "requires_approval": True,
                },
                requires_approval=True,
            )

        # Write the file
        try:
            resolved.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolOutput(success=False, error=f"Write failed: {exc}")

        return ToolOutput(
            success=True,
            data={
                "action": "patch",
                "path": display_path,
                "diff": diff,
                "written": True,
                "preview": preview,
            },
            requires_approval=False,
            verification_needed=True,
        )


__all__ = [
    "FilesystemWriteTool",
]
