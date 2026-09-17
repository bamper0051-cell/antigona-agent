"""Day 24 tests: Read-only filesystem tool.

Verifies:
- _resolve_safe boundary enforcement
- FilesystemReadTool.list, read, search
- Boundary escape attempts (absolute, relative, symlink)
- Edge cases: empty dir, large dir, truncated results, missing files
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from antigona.tools.contracts import ToolInput
from antigona.tools.filesystem_read import FilesystemReadTool, _resolve_safe


class TestResolveSafe:
    """_resolve_safe boundary enforcement tests."""

    def test_resolve_safe_relative(self, tmp_path: Path) -> None:
        resolved = _resolve_safe(tmp_path, "sub/file.txt")
        assert str(resolved) == str((tmp_path / "sub/file.txt").resolve())

    def test_resolve_safe_dot(self, tmp_path: Path) -> None:
        resolved = _resolve_safe(tmp_path, ".")
        assert str(resolved) == str(tmp_path.resolve())

    def test_resolve_safe_empty(self, tmp_path: Path) -> None:
        resolved = _resolve_safe(tmp_path, "")
        assert str(resolved) == str(tmp_path.resolve())

    def test_resolve_safe_rejects_outside(self, tmp_path: Path) -> None:
        base = tmp_path / "boundary"
        base.mkdir()
        with pytest.raises(ValueError, match="outside boundary"):
            _resolve_safe(base, "../outside")

    def test_resolve_safe_rejects_absolute_path(self, tmp_path: Path) -> None:
        """Absolute paths must be rejected — no escape."""
        base = tmp_path / "boundary"
        base.mkdir()
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "passwd").write_text("data")
        with pytest.raises(ValueError, match=r"(boundary|unsafe|absolute|outside)"):
            _resolve_safe(base, "/etc/passwd")

    def test_resolve_safe_rejects_non_absolute_base(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            _resolve_safe(Path("relative/path"), "test")

    def test_resolve_safe_rejects_symlink_escape(self, tmp_path: Path) -> None:
        base = tmp_path / "boundary"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (base / "link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="outside boundary"):
            _resolve_safe(base, "link/secret.txt")


class TestFilesystemReadTool:
    """FilesystemReadTool behavior tests."""

    def test_spec(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        spec = tool.spec
        assert spec.name == "filesystem.read"
        assert spec.category.value == "filesystem_read"
        assert str(tmp_path.resolve()) in spec.allowed_targets

    def test_validate_invalid_action(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "invalid"})
        errors = tool.validate(inp)
        assert len(errors) >= 1
        assert "Invalid action" in errors[0]

    def test_validate_missing_path(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read"})
        errors = tool.validate(inp)
        assert any("path is required" in e for e in errors)

    def test_validate_valid(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "."})
        errors = tool.validate(inp)
        assert errors == []

    @pytest.mark.asyncio
    async def test_dry_run(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(
            tool_name="filesystem.read",
            params={"action": "list", "path": "."},
            dry_run=True,
        )
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("dry_run") is True

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "unknown"})
        out = await tool.execute(inp)
        assert not out.success
        assert "Unknown action" in (out.error or "")

    # ---- list ----

    @pytest.mark.asyncio
    async def test_list_empty_dir(self, tmp_path: Path) -> None:
        state_root = Path(os.environ["ANTIGONA_STATE_ROOT"])
        assert state_root.is_dir()
        assert state_root != tmp_path / "state"
        assert tmp_path not in state_root.parents
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "."})
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("items") == []
        assert out.data.get("total") == 0

    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")
        (tmp_path / "sub").mkdir()
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "."})
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("total") == 3
        names = {i["name"] for i in out.data["items"]}
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names

    @pytest.mark.asyncio
    async def test_list_nested(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "sub"})
        out = await tool.execute(inp)
        assert out.success
        assert len(out.data["items"]) == 1
        assert out.data["items"][0]["name"] == "nested.txt"

    @pytest.mark.asyncio
    async def test_list_not_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("data")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "file.txt"})
        out = await tool.execute(inp)
        assert not out.success
        assert "Not a directory" in (out.error or "")

    @pytest.mark.asyncio
    async def test_list_boundary_escape(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "../etc"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    @pytest.mark.asyncio
    async def test_list_truncated(self, tmp_path: Path) -> None:
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text(str(i))
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(
            tool_name="filesystem.read",
            params={"action": "list", "path": ".", "max_results": 3},
        )
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("total") == 3
        assert out.data.get("truncated") is True

    # ---- read ----

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("Hello, World!")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "hello.txt"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["content"] == "Hello, World!"

    @pytest.mark.asyncio
    async def test_read_nested_file(self, tmp_path: Path) -> None:
        sub = tmp_path / "deep"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested content")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "deep/nested.txt"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["content"] == "nested content"

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "nonexistent.txt"})
        out = await tool.execute(inp)
        assert not out.success
        assert "Not found" in (out.error or "") or "Not a file" in (out.error or "")

    @pytest.mark.asyncio
    async def test_read_boundary_escape(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "../etc/passwd"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    @pytest.mark.asyncio
    async def test_read_large_file(self, tmp_path: Path) -> None:
        content = "x" * (10 * 1024 * 1024 + 1)
        (tmp_path / "big.txt").write_text(content)
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "big.txt"})
        out = await tool.execute(inp)
        assert not out.success
        assert "too large" in (out.error or "").lower()

    # ---- search ----

    @pytest.mark.asyncio
    async def test_search_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("notes")
        (tmp_path / "data.csv").write_text("csv")
        (tmp_path / "other.log").write_text("log")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": "*.txt"})
        out = await tool.execute(inp)
        assert out.success
        assert len(out.data["items"]) == 1
        assert out.data["items"][0]["path"] == "notes.txt"

    @pytest.mark.asyncio
    async def test_search_no_match(self, tmp_path: Path) -> None:
        (tmp_path / "file.py").write_text("code")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": "*.rs"})
        out = await tool.execute(inp)
        assert out.success
        assert out.data["items"] == []

    @pytest.mark.asyncio
    async def test_search_empty_pattern(self, tmp_path: Path) -> None:
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": ""})
        out = await tool.execute(inp)
        assert not out.success
        assert "pattern is required" in (out.error or "")

    @pytest.mark.asyncio
    async def test_search_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a")
        sub = tmp_path / "deep"
        sub.mkdir()
        (sub / "b.py").write_text("b")
        tool = FilesystemReadTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": "*.py"})
        out = await tool.execute(inp)
        assert out.success
        paths = {i["path"] for i in out.data["items"]}
        # Wave 4 (E): Windows returns os.sep (backslash) paths — normalize.
        paths = {p.replace(os.sep, "/") for p in paths}
        assert "a.py" in paths
        assert "deep/b.py" in paths

    # ---- boundary security ----

    @pytest.mark.asyncio
    async def test_symlink_in_root_does_not_leak(self, tmp_path: Path) -> None:
        # Symlink points outside the BOUNDARY root (not just outside tmp_path)
        root = tmp_path / "workspace"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "leak.txt").write_text("leaked")
        (root / "link").symlink_to(outside, target_is_directory=True)

        tool = FilesystemReadTool(root_boundary=str(root))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "link/leak.txt"})
        out = await tool.execute(inp)
        assert not out.success
        assert "outside boundary" in (out.error or "")

    @pytest.mark.asyncio
    async def test_search_skips_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.py").write_text("secret")
        (root / "escape").symlink_to(outside, target_is_directory=True)

        tool = FilesystemReadTool(root_boundary=str(root))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": "*.py"})
        out = await tool.execute(inp)
        assert out.success
        # Should not find secret.py outside boundary
        paths = {i["path"] for i in out.data["items"]}
        assert "secret.py" not in paths

    # ---- default root boundary ----

    @pytest.mark.asyncio
    async def test_default_root_boundary(self) -> None:
        from antigona.core import paths
        tool = FilesystemReadTool()
        spec = tool.spec
        expected_root = str(paths.workspace_dir().resolve())
        assert expected_root in spec.allowed_targets[0]
        assert str(tool._root) == expected_root
