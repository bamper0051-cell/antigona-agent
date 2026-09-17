"""Terminal tool — subprocess execution with safety controls.

Provides controlled shell command execution with:
- Timeout enforcement
- Working directory (cwd) support
- Output cap (max characters)
- Protected command blocking
- Cancel-by-correlation-id support
"""

from __future__ import annotations

import asyncio.subprocess
import os
import shlex
from pathlib import Path

from antigona.tools.contracts import (
    RiskLevel,
    Tool,
    ToolCategory,
    ToolInput,
    ToolOutput,
    ToolSpec,
)

# Set of command prefixes that are ALWAYS blocked regardless of arguments.
# Each entry is checked as a prefix of the first token in the command.
PROTECTED_COMMANDS: frozenset[str] = frozenset({
    # System shutdown / reboot
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    # Service management
    "systemctl",
    "service",
    # Docker destructive operations
    "docker",
    # Disk and filesystem operations
    "mkfs",
    "mke2fs",
    "mkswap",
    "fdisk",
    "parted",
    "gdisk",
    "cfdisk",
    "sfdisk",
    "wipefs",
    "mount",
    "umount",
    "swapoff",
    "swapon",
    # Low-level I/O that can destroy data
    "dd",
    # Network configuration
    "iptables",
    "ip6tables",
    "ufw",
    "firewall-cmd",
    # User / account management
    "passwd",
    "chpasswd",
    "useradd",
    "userdel",
    "usermod",
    "groupadd",
    "groupdel",
    "chown",
    "chgrp",
    # Package management (disallowed for safety)
    "apt",
    "apt-get",
    "dpkg",
    "rpm",
    "yum",
    "dnf",
    "pacman",
    "snap",
    # Kernel / boot
    "kexec",
    "modprobe",
    "insmod",
    "rmmod",
    # Privilege escalation
    "sudo",
    "su",
    # Network namespaces / containers (beyond docker)
    "crictl",
    "nerdctl",
    "podman",
    "runc",
})

# Patterns checked within the FULL command string that are blocked.
BLOCKED_PATTERNS: list[tuple[str, str]] = [
    # Destructive recursive deletion
    ("rm -rf /", "destructive recursive removal"),
    ("rm -rf /*", "destructive recursive removal"),
    ("rm -rf ~", "destructive recursive home removal"),
    ("rm -rf .", "destructive recursive current directory"),
    ("rm -rf *", "destructive recursive wildcard"),
    # Force deletion
    ("rm -f /", "force removal of root"),
    # Chmod dangerous
    ("chmod 000", "permission removal that could lock files"),
    ("chmod -R 000", "recursive permission removal"),
    # Truncating devices
    ("> /dev/", "direct device write"),
    ("dd if=", "low-level disk write via dd"),
    # Git destructive
    ("git push --force", "force push that overwrites remote history"),
    ("git reset --hard", "hard reset that discards changes — allowed only with target"),
    # Docker destructive
    ("docker rm -f", "force remove Docker container"),
    ("docker rmi", "remove Docker image"),
    ("docker system prune", "Docker system prune removes all unused data"),
    ("docker volume rm", "remove Docker volume"),
    ("docker network rm", "remove Docker network"),
    ("docker kill", "kill Docker container"),
    # Systemctl destructive
    ("systemctl stop", "stop system service"),
    ("systemctl disable", "disable system service"),
    ("systemctl restart", "restart system service — use sudo in microVM only"),
    # Shutdown variants
    ("shutdown -h", "system halt"),
    ("shutdown -r", "system reboot"),
    ("shutdown -P", "system poweroff"),
    ("shutdown now", "immediate shutdown"),
]

DEFAULT_OUTPUT_CAP = 100_000  # 100KB max output
MAX_OUTPUT_CAP = 1_000_000  # 1MB absolute max


def _is_protected(command: str) -> tuple[bool, str]:
    """Check if a command is protected (blocked).

    Args:
        command: The full command string.

    Returns:
        Tuple of (is_blocked, reason). If not blocked, reason is empty string.
    """
    lower_cmd = command.lower().strip()

    # Skip empty commands
    if not lower_cmd:
        return False, ""

    # Extract the first token (the binary name)
    try:
        tokens = shlex.split(lower_cmd)
    except ValueError:
        # If shlex can't parse it, check raw
        tokens = lower_cmd.split()

    if not tokens:
        return False, ""

    binary = tokens[0].lstrip("/")

    # Check if the binary name (or its prefix) is in PROTECTED_COMMANDS
    if binary in PROTECTED_COMMANDS:
        return True, f"Command '{binary}' is protected and cannot be executed"

    # Also check prefix matching (e.g., "mkfs.ext4" matches "mkfs")
    for protected in PROTECTED_COMMANDS:
        if binary == protected or binary.startswith(protected + "."):
            return True, f"Command '{binary}' is protected (matches '{protected}') and cannot be executed"

    # Check for blocked patterns in the full command
    for pattern, reason in BLOCKED_PATTERNS:
        if pattern in lower_cmd:
            return True, f"Blocked pattern found: {pattern} — {reason}"

    return False, ""


# Global dict to track cancellable processes by correlation_id
_running_processes: dict[str, asyncio.subprocess.Process] = {}


class TerminalTool(Tool):
    """Terminal tool for controlled shell command execution.

    Provides:
    - run: Execute a shell command with timeout and cwd support.
    - cancel: Cancel a running command by correlation_id.

    Every command is checked against a list of protected commands and
    blocked patterns before execution. Output is capped to prevent
    resource exhaustion.
    """

    def __init__(self, root_boundary: str | None = None) -> None:
        # Default root boundary: env override, else the *real* home directory
        # from the single home resolver (ADR-007). Historically the literal
        # "/root" was baked in, so off-host the default pointed at a directory
        # that does not exist and ignored ANTIGONA_HOME_DIR. On this host
        # home_dir() == "/root", so behaviour is unchanged.
        from antigona.core.paths import home_dir

        default_root = os.environ.get("ANTIGONA_WORKSPACE") or str(home_dir())
        self._root = Path(root_boundary or default_root).resolve()
        self._root_str = str(self._root)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="terminal",
            category=ToolCategory.SHELL,
            description="Execute shell commands with timeout, cwd, output cap, protected-command blocking",
            risk_level=RiskLevel.HIGH,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["run", "cancel"],
                        "description": "Action to perform: run a command or cancel one by correlation_id",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command (relative to root boundary, default root)",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30, max: 300)",
                        "default": 30,
                    },
                    "output_cap": {
                        "type": "integer",
                        "description": "Max output characters (default: 100000, max: 1000000)",
                        "default": 100_000,
                    },
                    "correlation_id": {
                        "type": "string",
                        "description": "Correlation ID for cancellation support",
                    },
                },
                "required": ["action"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "cancelled": {"type": "boolean"},
                    "truncated": {"type": "boolean"},
                    "command": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                },
            },
            allowed_targets=[str(self._root)],
            requires_approval=True,
            timeout_seconds=60,
        )

    def validate(self, inp: ToolInput) -> list[str]:
        errors: list[str] = []
        action = inp.params.get("action", "")

        if action not in ("run", "cancel"):
            errors.append(f"Invalid action: '{action}'. Must be 'run' or 'cancel'")
            return errors

        if action == "run":
            command = inp.params.get("command", "")
            if not command or not command.strip():
                errors.append("command is required for run action")

            timeout = inp.params.get("timeout_seconds", 30)
            if isinstance(timeout, (int, float)) and timeout > 300:
                errors.append("timeout_seconds must not exceed 300")

            output_cap = inp.params.get("output_cap", DEFAULT_OUTPUT_CAP)
            if isinstance(output_cap, (int, float)) and output_cap > MAX_OUTPUT_CAP:
                errors.append(f"output_cap must not exceed {MAX_OUTPUT_CAP}")

            # Check cwd is within root boundary
            cwd = inp.params.get("cwd")
            if cwd:
                resolved_cwd = (self._root / cwd.lstrip("/")).resolve()
                if not str(resolved_cwd).startswith(self._root_str):
                    errors.append(f"cwd '{cwd}' resolves outside root boundary")

        elif action == "cancel":
            if not inp.params.get("correlation_id"):
                errors.append("correlation_id is required for cancel action")

        return errors

    async def execute(self, inp: ToolInput) -> ToolOutput:
        action = inp.params.get("action", "")

        if action == "run":
            return await self._run_command(inp)
        elif action == "cancel":
            return self._cancel_command(inp)
        else:
            return ToolOutput(success=False, error=f"Unknown action: {action}")

    async def _run_command(self, inp: ToolInput) -> ToolOutput:
        command = inp.params.get("command", "").strip()
        timeout = int(inp.params.get("timeout_seconds", 30))
        output_cap = int(inp.params.get("output_cap", DEFAULT_OUTPUT_CAP))
        cwd_param = inp.params.get("cwd", "")
        correlation_id = inp.params.get("correlation_id", inp.correlation_id or "")

        if not command:
            return ToolOutput(success=False, error="command is required")

        # Check protected commands BEFORE execution
        blocked, reason = _is_protected(command)
        if blocked:
            return ToolOutput(
                success=False,
                error=f"Command blocked: {reason}",
                data={"command": command, "blocked": True},
            )

        if inp.dry_run:
            return ToolOutput(
                success=True,
                data={
                    "action": "run",
                    "command": command,
                    "dry_run": True,
                    "requires_approval": True,
                    "blocked_patterns_checked": True,
                },
                requires_approval=True,
            )

        # Resolve cwd
        if cwd_param:
            cwd = (self._root / cwd_param.lstrip("/")).resolve()
            # Ensure cwd is within boundary
            if not str(cwd).startswith(self._root_str):
                return ToolOutput(
                    success=False,
                    error=f"cwd '{cwd_param}' resolves outside root boundary",
                )
            if not cwd.is_dir():
                return ToolOutput(
                    success=False,
                    error=f"cwd '{cwd_param}' is not a directory or does not exist",
                )
        else:
            cwd = self._root

        # Execute with subprocess
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )

            # Register for cancellation
            if correlation_id:
                _running_processes[correlation_id] = process

            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                if correlation_id and correlation_id in _running_processes:
                    del _running_processes[correlation_id]
                return ToolOutput(
                    success=False,
                    error=f"Command timed out after {timeout}s",
                    data={
                        "command": command,
                        "exit_code": -1,
                        "timed_out": True,
                        "timeout_seconds": timeout,
                    },
                )

            finally:
                if correlation_id and correlation_id in _running_processes:
                    del _running_processes[correlation_id]

            stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
            stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

            exit_code = process.returncode or 0

            # Apply output cap
            truncated = False
            if len(stdout) > output_cap:
                stdout = stdout[:output_cap] + "\n... [output truncated]"
                truncated = True
            if len(stderr) > output_cap:
                stderr = stderr[:output_cap] + "\n... [stderr truncated]"
                truncated = True

            return ToolOutput(
                success=exit_code == 0,
                data={
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "command": command,
                    "cwd": str(cwd),
                    "truncated": truncated,
                },
                error=None if exit_code == 0 else f"Command exited with code {exit_code}",
                verification_needed=True,
            )

        except FileNotFoundError as exc:
            return ToolOutput(success=False, error=f"Command not found: {exc}")
        except PermissionError as exc:
            return ToolOutput(success=False, error=f"Permission denied: {exc}")
        except OSError as exc:
            return ToolOutput(success=False, error=f"OS error: {exc}")

    def _cancel_command(self, inp: ToolInput) -> ToolOutput:
        correlation_id = inp.params.get("correlation_id", "")

        if not correlation_id:
            return ToolOutput(success=False, error="correlation_id is required")

        process = _running_processes.get(correlation_id)
        if process is None:
            return ToolOutput(
                success=False,
                error=f"No running process found for correlation_id '{correlation_id}'",
            )

        try:
            process.kill()
            return ToolOutput(
                success=True,
                data={
                    "action": "cancel",
                    "correlation_id": correlation_id,
                    "cancelled": True,
                    "message": f"Process for correlation_id '{correlation_id}' has been killed",
                },
            )
        except ProcessLookupError:
            # Process already exited
            return ToolOutput(
                success=True,
                data={
                    "action": "cancel",
                    "correlation_id": correlation_id,
                    "cancelled": False,
                    "message": "Process already exited",
                },
            )
        finally:
            if correlation_id in _running_processes:
                del _running_processes[correlation_id]


__all__ = [
    "TerminalTool",
    "_is_protected",
    "PROTECTED_COMMANDS",
    "BLOCKED_PATTERNS",
]
