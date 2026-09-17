"""Day 25 tests: Write filesystem tool — create_file, edit_file, patch, diff preview.

Verifies:
- create_file: creates new file, rejects overwrites, generates diff preview
- edit_file: replaces content, detects no-change, generates diff preview
- patch: find-replace, reports old_string not found
- Root boundary enforcement (same as _resolve_safe from filesystem_read)
- Dry run produces preview without writing
- requires_approval=True returned before write
- verification_needed=True after successful write
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tools.contracts import ToolInput
from antigona.tools.filesystem_write import FilesystemWriteTool


class TestFilesystemWriteTool:
    """FilesystemWriteTool behavior tests."""

    def test_spec(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        spec = tool.spec
        assert spec.name == "filesystem.write"
        assert spec.category.value == "filesystem_write"
        assert spec.risk_level.value == "HIGH"
        assert spec.requires_approval is True
        assert str(tmp_path.resolve()) in spec.allowed_targets

    # ---- validate ----

    def test_validate_invalid_action(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "invalid", "path": "test.txt"})
        errors = tool.validate(inp)
        assert any("Invalid action" in e for e in errors)

    def test_validate_missing_path(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file"})
        errors = tool.validate(inp)
        assert any("path is required" in e for e in errors)

    def test_validate_absolute_path_rejected(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "/etc/passwd"})
        errors = tool.validate(inp)
        assert any("relative" in e for e in errors)

    def test_validate_create_no_content(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "test.txt"})
        errors = tool.validate(inp)
        assert any("content is required" in e for e in errors)

    def test_validate_edit_no_content(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "test.txt"})
        errors = tool.validate(inp)
        assert any("content is required" in e for e in errors)

    def test_validate_patch_no_old_string(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "test.txt", "new_string": "world"})
        errors = tool.validate(inp)
        assert any("old_string is required" in e for e in errors)

    def test_validate_patch_no_new_string(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "test.txt", "old_string": "hello"})
        errors = tool.validate(inp)
        assert any("new_string is required" in e for e in errors)

    def test_validate_patch_empty_old_string(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "test.txt", "old_string": "", "new_string": "world"})
        errors = tool.validate(inp)
        assert any("must not be empty" in e for e in errors)

    def test_validate_valid_create(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "test.txt", "content": "hello"})
        errors = tool.validate(inp)
        assert errors == []

    def test_validate_valid_edit(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "test.txt", "content": "new"})
        errors = tool.validate(inp)
        assert errors == []

    def test_validate_valid_patch(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "test.txt", "old_string": "a", "new_string": "b"})
        errors = tool.validate(inp)
        assert errors == []

    # ---- create_file ----

    @pytest.mark.asyncio
    async def test_create_file_success(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "hello.txt", "content": "Hello, World!"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["action"] == "create_file"
        assert out.data["written"] is True
        assert out.verification_needed is True
        # Verify the file was actually created
        assert (tmp_path / "hello.txt").read_text() == "Hello, World!"

    @pytest.mark.asyncio
    async def test_create_file_dry_run(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "hello.txt", "content": "Hello"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.data["dry_run"] is True
        assert out.data["written"] is False
        assert out.requires_approval is True
        # File should NOT have been created
        assert not (tmp_path / "hello.txt").exists()

    @pytest.mark.asyncio
    async def test_create_file_preview_contains_diff(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "new.txt", "content": "line1\nline2\n"})
        out = await tool.execute(inp)
        assert out.success
        preview = out.data["preview"]
        assert "CREATE" in preview
        assert "new.txt" in preview
        assert "+line1" in preview
        assert "+line2" in preview

    @pytest.mark.asyncio
    async def test_create_file_overwrite_fails(self, tmp_path: Path) -> None:
        (tmp_path / "existing.txt").write_text("original")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "existing.txt", "content": "new"})
        out = await tool.execute(inp)
        assert not out.success
        assert "already exists" in (out.error or "")

    @pytest.mark.asyncio
    async def test_create_file_nonexistent_parent(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "missing_dir/file.txt", "content": "data"})
        out = await tool.execute(inp)
        assert not out.success
        assert "Parent directory" in (out.error or "")

    @pytest.mark.asyncio
    async def test_create_file_boundary_escape(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "../outside.txt", "content": "data"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    # ---- edit_file ----

    @pytest.mark.asyncio
    async def test_edit_file_success(self, tmp_path: Path) -> None:
        (tmp_path / "editable.txt").write_text("original content")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "editable.txt", "content": "updated content"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["written"] is True
        assert out.verification_needed is True
        assert (tmp_path / "editable.txt").read_text() == "updated content"

    @pytest.mark.asyncio
    async def test_edit_file_dry_run(self, tmp_path: Path) -> None:
        (tmp_path / "editable.txt").write_text("original content")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "editable.txt", "content": "updated content"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.data["dry_run"] is True
        assert out.data["written"] is False
        # File should be unchanged
        assert (tmp_path / "editable.txt").read_text() == "original content"

    @pytest.mark.asyncio
    async def test_edit_file_no_change(self, tmp_path: Path) -> None:
        (tmp_path / "same.txt").write_text("identical")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "same.txt", "content": "identical"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["written"] is False
        assert out.data.get("unchanged") is True

    @pytest.mark.asyncio
    async def test_edit_file_missing(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "nonexistent.txt", "content": "data"})
        out = await tool.execute(inp)
        assert not out.success
        assert "does not exist" in (out.error or "")

    @pytest.mark.asyncio
    async def test_edit_file_preview_shows_diff(self, tmp_path: Path) -> None:
        (tmp_path / "diff_preview.txt").write_text("line1\nline2\n")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "diff_preview.txt", "content": "line1\nmodified\n"})
        out = await tool.execute(inp)
        assert out.success
        preview = out.data["preview"]
        assert "EDIT" in preview
        assert "diff_preview.txt" in preview
        assert "-line2" in preview or "-line2" in out.data["diff"]
        assert "+modified" in preview or "+modified" in out.data["diff"]

    @pytest.mark.asyncio
    async def test_edit_file_boundary_escape(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "../etc/hostname", "content": "pwned"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    # ---- patch ----

    @pytest.mark.asyncio
    async def test_patch_success(self, tmp_path: Path) -> None:
        (tmp_path / "patchable.txt").write_text("Hello, world!")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "patchable.txt", "old_string": "world", "new_string": "there"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["written"] is True
        assert out.verification_needed is True
        assert (tmp_path / "patchable.txt").read_text() == "Hello, there!"

    @pytest.mark.asyncio
    async def test_patch_dry_run(self, tmp_path: Path) -> None:
        (tmp_path / "patchable.txt").write_text("original text")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "patchable.txt", "old_string": "original", "new_string": "updated"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.data["dry_run"] is True
        assert out.data["written"] is False
        assert (tmp_path / "patchable.txt").read_text() == "original text"

    @pytest.mark.asyncio
    async def test_patch_old_string_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "patchable.txt").write_text("Hello, world!")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "patchable.txt", "old_string": "nonexistent", "new_string": "replacement"})
        out = await tool.execute(inp)
        assert not out.success
        assert "not found" in (out.error or "")

    @pytest.mark.asyncio
    async def test_patch_missing_file(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "missing.txt", "old_string": "a", "new_string": "b"})
        out = await tool.execute(inp)
        assert not out.success
        assert "does not exist" in (out.error or "")

    @pytest.mark.asyncio
    async def test_patch_preview_shows_diff(self, tmp_path: Path) -> None:
        (tmp_path / "patch_diff.txt").write_text("Hello Alice")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "patch_diff.txt", "old_string": "Alice", "new_string": "Bob"})
        out = await tool.execute(inp)
        assert out.success
        preview = out.data["preview"]
        assert "PATCH" in preview
        assert "Alice" in preview and "Bob" in preview

    @pytest.mark.asyncio
    async def test_patch_only_first_occurrence(self, tmp_path: Path) -> None:
        (tmp_path / "multi.txt").write_text("a a a")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "multi.txt", "old_string": "a", "new_string": "X"})
        out = await tool.execute(inp)
        assert out.success
        assert (tmp_path / "multi.txt").read_text() == "X a a"

    @pytest.mark.asyncio
    async def test_patch_boundary_escape(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "../etc/shadow", "old_string": "a", "new_string": "b"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    # ---- symlink escape prevention ----

    @pytest.mark.asyncio
    async def test_create_blocked_via_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)

        tool = FilesystemWriteTool(root_boundary=str(root))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "escape/pwn.txt", "content": "malicious"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    @pytest.mark.asyncio
    async def test_edit_blocked_via_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "target.txt").write_text("safe")
        (root / "escape").symlink_to(outside, target_is_directory=True)

        tool = FilesystemWriteTool(root_boundary=str(root))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "escape/target.txt", "content": "pwned"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    # ---- requires_approval on output ----

    @pytest.mark.asyncio
    async def test_create_requires_approval_before_write(self, tmp_path: Path) -> None:
        """When not dry_run, approval is NOT required on output (it was already approved)."""
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "approved_test.txt", "content": "test"})
        out = await tool.execute(inp)
        assert out.success
        # After execution, approval has been given — output shows no pending approval
        assert out.requires_approval is False

    @pytest.mark.asyncio
    async def test_dry_run_requires_approval(self, tmp_path: Path) -> None:
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "dry_test.txt", "content": "test"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.requires_approval is True

    # ---- nested path support ----

    @pytest.mark.asyncio
    async def test_create_file_nested_subdirectory(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "create_file", "path": "subdir/nested.txt", "content": "nested"})
        out = await tool.execute(inp)
        assert out.success
        assert (tmp_path / "subdir" / "nested.txt").read_text() == "nested"

    @pytest.mark.asyncio
    async def test_edit_file_nested(self, tmp_path: Path) -> None:
        sub = tmp_path / "deep"
        sub.mkdir()
        (sub / "target.txt").write_text("old")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "edit_file", "path": "deep/target.txt", "content": "new"})
        out = await tool.execute(inp)
        assert out.success
        assert (tmp_path / "deep" / "target.txt").read_text() == "new"

    @pytest.mark.asyncio
    async def test_patch_nested_file(self, tmp_path: Path) -> None:
        sub = tmp_path / "nested"
        sub.mkdir()
        (sub / "config.txt").write_text("KEY=old_value")
        tool = FilesystemWriteTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.write", params={"action": "patch", "path": "nested/config.txt", "old_string": "old_value", "new_string": "new_value"})
        out = await tool.execute(inp)
        assert out.success
        assert (tmp_path / "nested" / "config.txt").read_text() == "KEY=new_value"
