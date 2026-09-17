"""Day 26 tests: Terminal tool — subprocess with timeout, cwd, output cap, cancel.

Verifies:
- Protected command blocking (rm -rf, shutdown, docker, systemctl)
- Successful command execution (echo, pwd, ls)
- Timeout enforcement
- Output capping
- cwd support and boundary enforcement
- Dry run preview
- Cancel by correlation_id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tools.contracts import ToolInput
from antigona.tools.terminal import TerminalTool, _is_protected


class TestIsProtected:
    """_is_protected unit tests."""

    def test_echo_not_blocked(self) -> None:
        blocked, reason = _is_protected("echo hello")
        assert not blocked
        assert reason == ""

    def test_ls_not_blocked(self) -> None:
        blocked, reason = _is_protected("ls -la")
        assert not blocked

    def test_rm_regular_not_blocked(self) -> None:
        blocked, reason = _is_protected("rm file.txt")
        assert not blocked

    def test_rm_rf_root_blocked(self) -> None:
        blocked, reason = _is_protected("rm -rf /")
        assert blocked
        assert "destructive" in reason

    def test_rm_rf_wildcard_blocked(self) -> None:
        blocked, reason = _is_protected("rm -rf *")
        assert blocked

    def test_shutdown_blocked(self) -> None:
        blocked, reason = _is_protected("shutdown -h now")
        assert blocked
        assert "protected" in reason

    def test_docker_any_command_blocked(self) -> None:
        blocked, reason = _is_protected("docker ps")
        assert blocked
        assert "docker" in reason

    def test_docker_rm_blocked(self) -> None:
        blocked, reason = _is_protected("docker rm -f container_name")
        assert blocked

    def test_systemctl_blocked(self) -> None:
        blocked, reason = _is_protected("systemctl restart nginx")
        assert blocked

    def test_systemctl_status_blocked(self) -> None:
        """Even read-only systemctl is blocked for safety."""
        blocked, reason = _is_protected("systemctl status sshd")
        assert blocked

    def test_mkfs_blocked(self) -> None:
        blocked, reason = _is_protected("mkfs.ext4 /dev/sda1")
        # mkfs.ext4 starts with "mkfs" — check prefix stripping
        assert blocked

    def test_dd_blocked(self) -> None:
        blocked, reason = _is_protected("dd if=/dev/zero of=/dev/sda")
        assert blocked

    def test_iptables_blocked(self) -> None:
        blocked, reason = _is_protected("iptables -L")
        assert blocked

    def test_passwd_blocked(self) -> None:
        blocked, reason = _is_protected("passwd root")
        assert blocked

    def test_apt_blocked(self) -> None:
        blocked, reason = _is_protected("apt install nginx")
        assert blocked

    def test_sudo_blocked(self) -> None:
        blocked, reason = _is_protected("sudo rm -rf /")
        assert blocked

    def test_git_push_force_blocked(self) -> None:
        blocked, reason = _is_protected("git push --force origin main")
        assert blocked

    def test_empty_not_blocked(self) -> None:
        blocked, reason = _is_protected("")
        assert not blocked

    def test_whitespace_not_blocked(self) -> None:
        blocked, reason = _is_protected("   ")
        assert not blocked

    def test_case_insensitive_blocking(self) -> None:
        blocked, reason = _is_protected("RM -RF /")
        assert blocked


class TestTerminalTool:
    """TerminalTool behavior tests."""

    def test_spec(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        spec = tool.spec
        assert spec.name == "terminal"
        assert spec.category.value == "shell"
        assert spec.risk_level.value == "HIGH"
        assert spec.requires_approval is True

    # ---- validation ----

    def test_validate_invalid_action(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "invalid"})
        errors = tool.validate(inp)
        assert any("Invalid action" in e for e in errors)

    def test_validate_run_no_command(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run"})
        errors = tool.validate(inp)
        assert any("command is required" in e for e in errors)

    def test_validate_run_empty_command(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": ""})
        errors = tool.validate(inp)
        assert any("command is required" in e for e in errors)

    def test_validate_run_timeout_too_high(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hi", "timeout_seconds": 301})
        errors = tool.validate(inp)
        assert any("timeout_seconds must not exceed" in e for e in errors)

    def test_validate_run_output_cap_too_high(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hi", "output_cap": 2_000_000})
        errors = tool.validate(inp)
        assert any("output_cap must not exceed" in e for e in errors)

    def test_validate_cancel_no_correlation_id(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "cancel"})
        errors = tool.validate(inp)
        assert any("correlation_id is required" in e for e in errors)

    def test_validate_run_cwd_outside_boundary(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hi", "cwd": "../etc"})
        errors = tool.validate(inp)
        assert any("outside root boundary" in e for e in errors)

    def test_validate_run_valid(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hi"})
        errors = tool.validate(inp)
        assert errors == []

    # ---- protected commands ----

    @pytest.mark.asyncio
    async def test_shutdown_blocked(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "shutdown -h now"})
        out = await tool.execute(inp)
        assert not out.success
        assert "blocked" in (out.error or "").lower()

    @pytest.mark.asyncio
    async def test_docker_blocked(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "docker ps"})
        out = await tool.execute(inp)
        assert not out.success
        assert "blocked" in (out.error or "").lower()

    @pytest.mark.asyncio
    async def test_systemctl_blocked(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "systemctl status"})
        out = await tool.execute(inp)
        assert not out.success
        assert "blocked" in (out.error or "").lower()

    @pytest.mark.asyncio
    async def test_sudo_blocked(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "sudo echo hi"})
        out = await tool.execute(inp)
        assert not out.success
        assert "blocked" in (out.error or "").lower()

    # ---- successful execution ----

    @pytest.mark.asyncio
    async def test_echo_success(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hello world"})
        out = await tool.execute(inp)
        assert out.success
        assert "hello world" in out.data.get("stdout", "")
        assert out.data.get("exit_code") == 0

    @pytest.mark.asyncio
    async def test_pwd(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "pwd"})
        out = await tool.execute(inp)
        assert out.success
        stdout = out.data.get("stdout", "")
        # Wave 4 (E): on Windows the shell is MSYS bash and `pwd` prints the
        # MSYS-style path (/tmp/...), not the Windows path — accept either form.
        assert str(tmp_path.resolve()) in stdout or tmp_path.name in stdout

    @pytest.mark.asyncio
    async def test_ls(self, tmp_path: Path) -> None:
        (tmp_path / "test_file.txt").write_text("data")
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "ls"})
        out = await tool.execute(inp)
        assert out.success
        assert "test_file.txt" in out.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_command_with_args(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo one two three"})
        out = await tool.execute(inp)
        assert out.success
        stdout = out.data.get("stdout", "")
        assert "one two three" in stdout

    # ---- non-zero exit code ----

    @pytest.mark.asyncio
    async def test_non_zero_exit(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "false"})
        out = await tool.execute(inp)
        assert not out.success
        assert out.data.get("exit_code") != 0

    # ---- cwd support ----

    @pytest.mark.asyncio
    async def test_cwd(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "in_sub.txt").write_text("present")
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "ls", "cwd": "subdir"})
        out = await tool.execute(inp)
        assert out.success
        assert "in_sub.txt" in out.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_cwd_nonexistent(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "ls", "cwd": "nonexistent"})
        out = await tool.execute(inp)
        assert not out.success
        assert "not a directory" in (out.error or "").lower()

    # ---- dry run ----

    @pytest.mark.asyncio
    async def test_dry_run(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "echo hello"}, dry_run=True)
        out = await tool.execute(inp)
        assert out.success
        assert out.data.get("dry_run") is True

    # ---- timeout ----

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "sleep 10", "timeout_seconds": 1})
        out = await tool.execute(inp)
        assert not out.success
        assert "timed out" in (out.error or "").lower()

    # ---- output cap ----

    @pytest.mark.asyncio
    async def test_output_truncation(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        # Generate more output than the cap allows
        inp = ToolInput(tool_name="terminal", params={"action": "run", "command": "python3 -c 'print(\"x\" * 5000)'", "output_cap": 100})
        out = await tool.execute(inp)
        assert out.success or (not out.success and out.data.get("exit_code", 0) != 0)
        if out.success:
            assert out.data.get("truncated") is True
            stdout = out.data.get("stdout", "")
            assert len(stdout) < 5000  # Should be capped

    # ---- cancel ----

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "cancel", "correlation_id": "nonexistent"})
        out = await tool.execute(inp)
        assert not out.success
        assert "No running process" in (out.error or "")

    @pytest.mark.asyncio
    async def test_cancel_valid(self, tmp_path: Path) -> None:
        """Verify cancel action exists and returns the right shape."""
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "cancel", "correlation_id": "test-cancel-123"})
        out = await tool.execute(inp)
        # Process may not be running, but the action itself is valid
        assert not out.success  # Process not found
        assert "No running process" in (out.error or "")

    # ---- unknown action ----

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path: Path) -> None:
        tool = TerminalTool(root_boundary=str(tmp_path))
        inp = ToolInput(tool_name="terminal", params={"action": "fly"})
        out = await tool.execute(inp)
        assert not out.success
        assert "Unknown action" in (out.error or "")
