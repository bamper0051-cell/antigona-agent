from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from antigona.egress import EgressProxy, EgressUnavailableError
from antigona.worker.tools import (
    DisabledWebFetchTool,
    ToolError,
    WebFetchTool,
    WorkspaceFileTools,
    WorkspaceGuard,
    WorkspaceShellTool,
)


def test_workspace_file_tools_confine_paths(tmp_path: Path) -> None:
    tools = WorkspaceFileTools(WorkspaceGuard(tmp_path))
    result = tools.write_text("a/b.txt", "hello")
    assert result.content == "hello"
    assert (tmp_path / "a" / "b.txt").read_text(encoding="utf-8") == "hello"
    with pytest.raises(ToolError):
        tools.write_text("../escape.txt", "x")


def test_shell_tool_blocks_non_allowlisted_commands(tmp_path: Path) -> None:
    shell = WorkspaceShellTool(WorkspaceGuard(tmp_path))
    with pytest.raises(ToolError):
        shell.run(["curl", "https://example.com"])


def test_shell_tool_normalizes_single_string_argv(tmp_path: Path) -> None:
    """LLM often emits command as one string with spaces — split into argv."""
    shell = WorkspaceShellTool(WorkspaceGuard(tmp_path))
    res = shell.run(["echo WIP-SHELL-CHECK"])
    assert res.exit_code == 0
    assert "WIP-SHELL-CHECK" in res.stdout
    assert res.command == ("echo", "WIP-SHELL-CHECK")


def test_shell_tool_still_blocks_string_with_unknown_binary(tmp_path: Path) -> None:
    shell = WorkspaceShellTool(WorkspaceGuard(tmp_path))
    with pytest.raises(ToolError):
        shell.run(["curl https://example.com"])


def test_web_fetch_tool_enabled_via_mock_proxy() -> None:
    mock_proxy = MagicMock(spec=EgressProxy)
    mock_proxy.fetch.return_value = "<html>Hello World</html>"

    tool = WebFetchTool(proxy=mock_proxy)
    result = tool.fetch("https://example.com/page")

    assert result.enabled is True
    assert result.detail == "<html>Hello World</html>"
    assert result.untrusted is True
    assert result.blocked is False
    mock_proxy.fetch.assert_called_once_with("https://example.com/page")


def test_web_fetch_tool_blocked_on_egress_error() -> None:
    mock_proxy = MagicMock(spec=EgressProxy)
    mock_proxy.fetch.side_effect = EgressUnavailableError("domain not in allowlist: evil.com")

    tool = WebFetchTool(proxy=mock_proxy)
    result = tool.fetch("https://evil.com")

    assert result.enabled is False
    assert "egress blocked: domain not in allowlist: evil.com" in result.detail
    assert result.untrusted is True
    assert result.blocked is True


def test_disabled_web_fetch_tool_backward_compatibility() -> None:
    tool = DisabledWebFetchTool()
    result = tool.fetch("https://example.com")
    assert result.enabled is False
    assert "egress blocked" in result.detail
    assert result.blocked is True


def test_web_fetch_tool_empty_url() -> None:
    tool = WebFetchTool()
    with pytest.raises(ToolError, match="url must not be empty"):
        tool.fetch("")
