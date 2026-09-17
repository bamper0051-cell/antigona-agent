from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ...observability import event
from ...sandbox.docker_sandbox import DockerSandboxBackend
from ...sandbox.microvm import (
    MicroVMProfile,
    MicroVMRunner,
    MicroVMUnavailableError,
)
from ...tools.shell_command import strip_shell_tool_prefix_argv
from .common import ToolError, WorkspaceGuard

LOGGER = logging.getLogger("antigona")

# ── Host allowlist ──────────────────────────────────────────────────────────
# ``cwd=`` confines nothing, so the host branch is only as safe as its argv.
# Commands that interpret an argument as a program to run or as a destination
# to overwrite (``find -exec``, ``sed -i``, ``cp``, ``mv``, ``rm``) are NOT
# host-executable: they route through the sandbox branch, which requires owner
# approval. What stays here either ignores its arguments entirely or has every
# argument confined to the workspace by ``_assert_confined`` below.
HOST_INERT_COMMANDS: frozenset[str] = frozenset({"echo", "pwd", "true"})

HOST_PATH_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "grep",
        "head",
        "ls",
        "mkdir",
        "tail",
        "touch",
        "wc",
    }
)

ALLOWED_COMMANDS: frozenset[str] = HOST_INERT_COMMANDS | HOST_PATH_COMMANDS

# A bare option (``-n``, ``-rf``, ``--color``). Carries no path of its own, so
# it needs no containment check; ``--opt=value`` is split and the value checked.
_BARE_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class ShellToolResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    untrusted: bool = False


class WorkspaceShellTool:
    def __init__(
        self,
        guard: WorkspaceGuard,
        timeout_seconds: int = 10,
        sandbox_runtime: str = "docker",
        microvm: MicroVMRunner | None = None,
    ) -> None:
        self.guard = guard
        self.timeout_seconds = timeout_seconds
        self.sandbox_runtime = sandbox_runtime.strip().lower()
        self.microvm = microvm
        self._docker_backend: DockerSandboxBackend | None = None

    def _assert_confined(self, arg: str) -> None:
        """Reject an argv element that can reach outside the workspace.

        Absolute paths, ``~`` expansions and ``..`` components are refused
        outright; everything else goes through ``WorkspaceGuard.resolve()``,
        which also rejects symlinked path components and anything that resolves
        outside the workspace root.
        """
        if not arg:
            return
        if arg.startswith("~"):
            raise ToolError("home-relative argument is forbidden on the host")
        raw = Path(arg)
        if raw.is_absolute():
            raise ToolError("absolute path argument is forbidden on the host")
        parts = [part for part in raw.parts if part != "."]
        if ".." in parts:
            raise ToolError("path traversal argument is forbidden on the host")
        if not parts:
            # "." / "./" — the workspace root itself, which is in bounds.
            return
        self.guard.resolve(str(Path(*parts)))

    def _validate_host_argv(self, command: Sequence[str]) -> None:
        """Every argument of a host command must be workspace-confined."""
        for arg in command[1:]:
            if _BARE_FLAG_RE.match(arg):
                continue
            if arg.startswith("-") and "=" in arg:
                self._assert_confined(arg.split("=", 1)[1])
                continue
            if arg.startswith("-"):
                raise ToolError(
                    "unrecognised option form is forbidden on the host branch"
                )
            self._assert_confined(arg)

    def run(
        self,
        command: Sequence[str],
        *,
        approved: bool = False,
        correlation_id: str = "",
        task_id: str = "",
    ) -> ShellToolResult:
        if not command:
            raise ToolError("command must not be empty")
        command = strip_shell_tool_prefix_argv(command)
        if not command:
            raise ToolError("command must not be empty")
        # Normalize a single string argv (e.g. ``["echo WIP-SHELL-CHECK"]``)
        # into a real argv list so allowlist checks see the binary, not the
        # whole command line.  Safe: shlex never executes anything.
        if len(command) == 1 and isinstance(command[0], str) and " " in command[0].strip():
            import shlex

            try:
                parts = shlex.split(command[0].strip())
            except ValueError:
                parts = []
            if parts:
                command = parts
        binary = command[0]
        is_low_risk = ("/" not in binary) and (binary in ALLOWED_COMMANDS)

        if is_low_risk:
            if binary in HOST_PATH_COMMANDS:
                self._validate_host_argv(command)
            process = subprocess.run(
                list(command),
                cwd=self.guard.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            return ShellToolResult(
                command=tuple(command),
                exit_code=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
                untrusted=False,
            )

        # High-risk route — NEVER on the host. Owner-approved high-risk commands run
        # in an isolated ephemeral Docker sandbox; without approval the command is
        # refused (P0 allowlist unchanged, fail-closed).
        if self.sandbox_runtime == "docker":
            if not approved:
                raise ToolError(
                    "high-risk command requires owner approval for sandboxed execution; "
                    "refusing to run on host (not in P0 allowlist)"
                )
            if self._docker_backend is None:
                # Sandbox runs (package installs) need more time than host
                # allowlisted commands; use a dedicated, longer budget.
                self._docker_backend = DockerSandboxBackend(
                    image="python:3.12-alpine",
                    workspace=self.guard.workspace,
                    timeout_seconds=max(self.timeout_seconds, 120),
                )
            sandbox_res = self._docker_backend.run(
                command,
                correlation_id=correlation_id,
                task_id=task_id,
            )
            return ShellToolResult(
                command=tuple(command),
                exit_code=sandbox_res.exit_code,
                stdout=sandbox_res.stdout,
                stderr=sandbox_res.stderr,
                untrusted=True,
            )

        if self.sandbox_runtime in {"firecracker", "e2b"} or self.microvm is not None:
            runner = self.microvm
            if runner is None:
                profile = MicroVMProfile(
                    workspace=self.guard.workspace,
                    timeout_seconds=self.timeout_seconds,
                )
                runner = MicroVMRunner.create(
                    profile=profile,
                    backend=self.sandbox_runtime,
                )
            if not runner.is_available():
                LOGGER.warning(
                    "micro-VM runtime %r is unavailable on host for high-risk command execution — blocking execution",
                    self.sandbox_runtime,
                )
                event(
                    "microvm_unavailable",
                    service="sandbox",
                    correlation_id=None,
                    status="blocked",
                    backend=self.sandbox_runtime,
                    reason="micro-VM runtime unavailable",
                )
                raise MicroVMUnavailableError(
                    f"micro-VM runtime {self.sandbox_runtime!r} is unavailable for high-risk tool execution"
                )

            runner.spawn()
            try:
                vm_res = runner.exec(command, timeout=self.timeout_seconds)
                return ShellToolResult(
                    command=tuple(command),
                    exit_code=vm_res.exit_code,
                    stdout=vm_res.stdout,
                    stderr=vm_res.stderr,
                    untrusted=True,
                )
            finally:
                runner.teardown()

        if "/" in binary:
            raise ToolError("absolute or relative binary paths are forbidden")
        raise ToolError(
            "high-risk command requires owner approval and a sandbox runtime; "
            "command is not in the P0 allowlist"
        )

