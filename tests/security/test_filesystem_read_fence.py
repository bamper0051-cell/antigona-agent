"""P5-010 Exploit probe and fence test matrix for FilesystemReadTool / _resolve_safe."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from antigona.core import paths
from antigona.tools.contracts import ToolInput
from antigona.tools.filesystem_read import FilesystemReadTool, _resolve_safe

EXPLOIT_PATHS = [
    pytest.param("..", id="dotdot"),
    pytest.param("../outside.txt", id="dotdot-outside"),
    pytest.param("sub/../../outside.txt", id="dotdot-nested"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param(f"{paths.home_dir()}/.bashrc", id="posix-absolute-root"),
    pytest.param("C:/Windows/System32", id="windows-drive-absolute"),
    pytest.param("C:\\Windows\\System32", id="windows-drive-backslash"),
    pytest.param("\\\\server\\share\\secret", id="windows-unc"),
    pytest.param("a/./b", id="normalize-dot-segment"),
    pytest.param("a//b", id="normalize-empty-segment"),
    pytest.param("a/b/", id="normalize-trailing-separator"),
    pytest.param("%2e%2e%2fsecret", id="percent-traversal"),
    pytest.param("..%2f..%2fsecret", id="percent-separator"),
    pytest.param("%252e%252e%252fsecret", id="double-percent"),
    pytest.param("％2e％2e％2fsecret", id="fullwidth-percent"),
    pytest.param("‥/secret", id="unicode-two-dot-leader"),
    pytest.param("．．／secret", id="unicode-fullwidth-traversal"),
    pytest.param("..\\..\\secret", id="backslash-traversal"),
    pytest.param("secret\x00.txt", id="null-byte-injection"),
    pytest.param("secret\n.txt", id="control-char-newline"),
]

NORMAL_SAFE_READS = [
    pytest.param("hello.txt", "Hello, World!", id="plain-file"),
    pytest.param("sub/nested.txt", "nested content", id="nested-file"),
    pytest.param("safe/Ａ.txt", "fullwidth content", id="fullwidth-letter"),
    pytest.param("file with spaces.txt", "spaces content", id="spaces"),
    pytest.param("файл.txt", "cyrillic content", id="cyrillic"),
    pytest.param("100%.txt", "percent content", id="literal-percent"),
    pytest.param("report．txt", "compat dot content", id="compat-dot"),
]


class DownstreamSpy:
    """Spies on downstream Path I/O methods to prove non-decorative zero-downstream execution."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: dict[str, list[tuple[str, tuple[Any, ...], dict[str, Any]]]] = {
            "stat": [],
            "is_file": [],
            "open": [],
            "read_text": [],
            "iterdir": [],
        }
        orig_stat = Path.stat
        orig_is_file = Path.is_file
        orig_open = Path.open
        orig_read_text = Path.read_text
        orig_iterdir = Path.iterdir

        def spy_stat(path_obj: Path, *args: Any, **kwargs: Any) -> Any:
            self.calls["stat"].append((str(path_obj), args, kwargs))
            return orig_stat(path_obj, *args, **kwargs)

        def spy_is_file(path_obj: Path, *args: Any, **kwargs: Any) -> bool:
            self.calls["is_file"].append((str(path_obj), args, kwargs))
            return orig_is_file(path_obj, *args, **kwargs)

        def spy_open(path_obj: Path, *args: Any, **kwargs: Any) -> Any:
            self.calls["open"].append((str(path_obj), args, kwargs))
            return orig_open(path_obj, *args, **kwargs)

        def spy_read_text(path_obj: Path, *args: Any, **kwargs: Any) -> str:
            self.calls["read_text"].append((str(path_obj), args, kwargs))
            return orig_read_text(path_obj, *args, **kwargs)

        def spy_iterdir(path_obj: Path, *args: Any, **kwargs: Any) -> Generator[Path, None, None]:
            self.calls["iterdir"].append((str(path_obj), args, kwargs))
            return orig_iterdir(path_obj, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", spy_stat)
        monkeypatch.setattr(Path, "is_file", spy_is_file)
        monkeypatch.setattr(Path, "open", spy_open)
        monkeypatch.setattr(Path, "read_text", spy_read_text)
        monkeypatch.setattr(Path, "iterdir", spy_iterdir)

    def reset(self) -> None:
        for k in self.calls:
            self.calls[k].clear()

    @property
    def total_calls(self) -> int:
        return sum(len(c) for c in self.calls.values())


@pytest.fixture
def spy_downstream_io(monkeypatch: pytest.MonkeyPatch) -> DownstreamSpy:
    return DownstreamSpy(monkeypatch)


class TestExploitProbesResolveSafe:
    """Probes verifying _resolve_safe denies all path traversal / exploit attempts."""

    @pytest.mark.parametrize("path", EXPLOIT_PATHS)
    def test_resolve_safe_rejects_all_exploit_paths(self, tmp_path: Path, path: str) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with pytest.raises(ValueError, match=r"(boundary|unsafe|absolute|outside|traversal)"):
            _resolve_safe(workspace, path)

    def test_resolve_safe_rejects_lexical_sibling_prefix(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        sibling = tmp_path / "workspace_sibling"
        workspace.mkdir()
        sibling.mkdir()
        (sibling / "secret.txt").write_text("leak")

        # Lexical sibling cannot be accessed by traversing out
        with pytest.raises(ValueError, match=r"(boundary|unsafe|outside|traversal)"):
            _resolve_safe(workspace, "../workspace_sibling/secret.txt")

    def test_resolve_safe_rejects_symlink_parent_escape(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (workspace / "escape_link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match=r"(boundary|outside|symlink)"):
            _resolve_safe(workspace, "escape_link/secret.txt")


class TestExploitProbesFilesystemReadTool:
    """Probes verifying FilesystemReadTool fails closed and never calls downstream reads."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", EXPLOIT_PATHS)
    async def test_tool_read_denies_exploit_with_zero_downstream_calls(
        self, tmp_path: Path, path: str, spy_downstream_io: DownstreamSpy
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = FilesystemReadTool(root_boundary=str(workspace))

        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": path})
        spy_downstream_io.reset()

        errors = tool.validate(inp)
        assert len(errors) > 0, f"Expected validation errors for {path!r}"
        out = await tool.execute(inp)

        assert not out.success
        # Downstream stat/is_file/open/read_text/iterdir must NEVER be called on exploit attempts
        assert (
            spy_downstream_io.total_calls == 0
        ), f"Expected zero downstream I/O calls for {path!r}, got: {spy_downstream_io.calls}"

    @pytest.mark.asyncio
    async def test_tool_read_denies_symlink_escape_with_zero_downstream_calls(
        self, tmp_path: Path, spy_downstream_io: DownstreamSpy
    ) -> None:
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (workspace / "escape_link").symlink_to(outside, target_is_directory=True)

        tool = FilesystemReadTool(root_boundary=str(workspace))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "escape_link/secret.txt"})
        spy_downstream_io.reset()

        out = await tool.execute(inp)
        assert not out.success
        # Zero file reading or stat on the target file
        assert len(spy_downstream_io.calls["is_file"]) == 0
        assert len(spy_downstream_io.calls["stat"]) == 0
        assert len(spy_downstream_io.calls["open"]) == 0
        assert len(spy_downstream_io.calls["read_text"]) == 0

    @pytest.mark.asyncio
    async def test_tool_read_denies_sibling_escape_with_zero_downstream_calls(
        self, tmp_path: Path, spy_downstream_io: DownstreamSpy
    ) -> None:
        workspace = tmp_path / "workspace"
        sibling = tmp_path / "workspace_sibling"
        workspace.mkdir()
        sibling.mkdir()
        (sibling / "secret.txt").write_text("leak")

        tool = FilesystemReadTool(root_boundary=str(workspace))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": "../workspace_sibling/secret.txt"})
        spy_downstream_io.reset()

        errors = tool.validate(inp)
        assert len(errors) > 0
        out = await tool.execute(inp)

        assert not out.success
        assert (
            spy_downstream_io.total_calls == 0
        ), f"Expected zero downstream I/O calls for sibling escape, got: {spy_downstream_io.calls}"

    @pytest.mark.asyncio
    async def test_tool_default_root_with_both_envs_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P5-010: Default workspace with both ANTIGONA_WORKSPACE and ANTIGONA_PROJECT_ROOT unset.

        Proves:
        1. paths.workspace_dir() resolves to authorized project workspace.
        2. It is strictly not /opt/antigona-home and not /.
        3. FilesystemReadTool() root is strictly paths.workspace_dir().resolve().
        4. Tool allowed_targets does not contain /opt/antigona-home.
        """
        monkeypatch.delenv("ANTIGONA_WORKSPACE", raising=False)
        monkeypatch.delenv("ANTIGONA_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)

        ws = paths.workspace_dir().resolve()
        assert str(ws) != "/opt/antigona-home"
        assert str(ws) != "/"
        assert ws.name == "workspace"
        assert ws.is_relative_to(paths.project_root().resolve())
        assert str(ws) == str((paths.project_root() / "workspace").resolve())

        tool = FilesystemReadTool()
        assert str(tool._root) != "/opt/antigona-home"
        assert str(tool._root) != "/"
        assert tool._root == ws
        assert tool.spec.allowed_targets == [str(ws)]

    @pytest.mark.asyncio
    async def test_tool_rejects_slash_root_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P5-010: Explicit home-directory or / must fail closed immediately.

        The home boundary is derived from the product resolver (ADR-007), never
        the literal ``/opt/antigona-home``: on a host whose ``HOME`` / ``ANTIGONA_HOME_DIR``
        is not ``/opt/antigona-home`` the literal would name a writable, non-forbidden path.
        """
        home_root = str(paths.home_dir())
        with pytest.raises(ValueError, match=r"Invalid workspace root boundary"):
            FilesystemReadTool(root_boundary=home_root)

        with pytest.raises(ValueError, match=r"Invalid workspace root boundary"):
            FilesystemReadTool(root_boundary="/")

        monkeypatch.setenv("ANTIGONA_WORKSPACE", home_root)
        with pytest.raises(ValueError, match=r"Invalid workspace root boundary"):
            FilesystemReadTool()

        monkeypatch.setenv("ANTIGONA_WORKSPACE", "/")
        with pytest.raises(ValueError, match=r"Invalid workspace root boundary"):
            FilesystemReadTool()

    @pytest.mark.asyncio
    async def test_tool_fails_closed_when_workspace_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When workspace root cannot be established, tool must fail closed."""
        monkeypatch.delenv("ANTIGONA_WORKSPACE", raising=False)
        monkeypatch.delenv("ANTIGONA_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
        monkeypatch.setattr(
            "antigona.core.paths.workspace_dir",
            lambda: (_ for _ in ()).throw(RuntimeError("no workspace")),
        )

        with pytest.raises((ValueError, RuntimeError), match=r"(workspace|fail-closed|authoritative)"):
            FilesystemReadTool()


class TestFilesystemReadToolNormalOperations:
    """Regression tests verifying valid in-workspace operations succeed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("path", "content"), NORMAL_SAFE_READS)
    async def test_read_valid_files_succeeds(
        self, tmp_path: Path, path: str, content: str
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        tool = FilesystemReadTool(root_boundary=str(workspace))
        inp = ToolInput(tool_name="filesystem.read", params={"action": "read", "path": path})
        out = await tool.execute(inp)

        assert out.success
        assert out.data["content"] == content
        assert out.data["size"] == len(content)

    @pytest.mark.asyncio
    async def test_list_valid_directory_succeeds(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "a.txt").write_text("a")
        (workspace / "sub").mkdir()
        (workspace / "sub" / "b.txt").write_text("b")

        tool = FilesystemReadTool(root_boundary=str(workspace))

        # List root with "."
        out_root = await tool.execute(
            ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "."})
        )
        assert out_root.success
        names = {item["name"] for item in out_root.data["items"]}
        assert "a.txt" in names
        assert "sub" in names

        # List sub
        out_sub = await tool.execute(
            ToolInput(tool_name="filesystem.read", params={"action": "list", "path": "sub"})
        )
        assert out_sub.success
        assert len(out_sub.data["items"]) == 1
        assert out_sub.data["items"][0]["name"] == "b.txt"

    @pytest.mark.asyncio
    async def test_search_valid_pattern_succeeds(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "a.py").write_text("a")
        (workspace / "sub").mkdir()
        (workspace / "sub" / "b.py").write_text("b")
        (workspace / "sub" / "c.txt").write_text("c")

        tool = FilesystemReadTool(root_boundary=str(workspace))
        out = await tool.execute(
            ToolInput(tool_name="filesystem.read", params={"action": "search", "pattern": "*.py"})
        )
        assert out.success
        found_paths = {item["path"].replace(os.sep, "/") for item in out.data["items"]}
        assert found_paths == {"a.py", "sub/b.py"}
