"""Owner-elevated host shell — PIN-gated, CLI-only, human-typed, fully audited.

Design constraints (Task 3 — see conversation record for the security review
that shaped this):

  * PIN-gated: only runs once the CLI's owner PIN gate
    (``antigona.cli_ui.layout.AntigonaLayout._request_owner_pin``) has
    succeeded for this process.
  * CLI-only: never wired into the Telegram channel or into any tool the
    model can invoke via ``⟪tool:...⟫`` syntax. Only a human typing
    ``/shell <command>`` at the CLI prompt can trigger it — the LLM never
    sees this capability, so it cannot be reached through prompt injection
    from web content, file contents, or a compromised task description.
  * Host-level: runs directly via subprocess, not inside the Docker sandbox
    (``antigona.shell.DockerShellTool``) or confined by
    ``antigona.worker.tools.common.WorkspaceGuard`` — that is the entire
    point of this tool (inspecting/operating on paths outside workspace,
    e.g. ``/etc``). Every invocation is logged for audit.

This does *not* replace the sandboxed ``sandbox.shell`` tool used by
task execution — that stays Docker-confined and approval-gated regardless
of owner mode, because it is reachable from model-driven task flows
(including, on Telegram, from a remote chat).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

from antigona.tools.shell_command import normalize_shell_command_first_token

logger = logging.getLogger(__name__)
AUDIT_LOGGER = logging.getLogger("antigona.audit.owner_shell")

_DEFAULT_TIMEOUT_SECONDS = 60
_OUTPUT_CAP = 65_536


class OwnerShellDenied(PermissionError):
    """Raised when the caller has not passed the CLI owner PIN gate."""


@dataclass(frozen=True)
class OwnerShellResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    truncated: bool


def run_owner_shell(
    command: str,
    *,
    is_owner: bool,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    output_cap: int = _OUTPUT_CAP,
) -> OwnerShellResult:
    """Run *command* on the host as the elevated owner.

    Args:
        command: Raw shell command, exactly as the human typed it.
        is_owner: Result of the CLI's PIN gate for this session
            (``AntigonaLayout.owner_mode``). Callers must pass the live
            value, not cache it — a session that never unlocked, or whose
            PIN attempts were exhausted, must hard-deny every call.
        timeout_seconds: Hard wall-clock limit; the process is killed and a
            ``subprocess.TimeoutExpired`` propagates on expiry.
        output_cap: Max bytes of stdout/stderr kept (each stream capped
            independently) to keep runaway output out of the chat log.

    Raises:
        OwnerShellDenied: *is_owner* is False.
    """
    stripped = command.strip()
    if not is_owner:
        AUDIT_LOGGER.warning("owner.shell DENIED (not elevated): %r", stripped)
        raise OwnerShellDenied(
            "owner mode is not active for this session — restart the CLI "
            "and enter the correct PIN to unlock /shell"
        )
    if not stripped:
        raise ValueError("command must not be empty")

    AUDIT_LOGGER.info("owner.shell exec: %r", stripped)
    # Case-insensitive leading-token normalization (UX): "Apt"/"Pwd"/"Ls"
    # typed with a capital letter resolve to the lowercase system binaries.
    # The audit log keeps the raw human-typed command; only the executed
    # string is normalized. See antigona/tools/shell_command.py.
    shell_cmd = normalize_shell_command_first_token(stripped)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", shell_cmd],
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
        )
    except subprocess.TimeoutExpired:
        AUDIT_LOGGER.warning("owner.shell TIMEOUT after %ss: %r", timeout_seconds, stripped)
        raise
    duration = time.monotonic() - started

    stdout, stdout_truncated = _cap(proc.stdout or "", output_cap)
    stderr, stderr_truncated = _cap(proc.stderr or "", output_cap)

    AUDIT_LOGGER.info(
        "owner.shell done: exit_code=%s duration=%.2fs command=%r",
        proc.returncode, duration, stripped,
    )
    return OwnerShellResult(
        command=stripped,
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        truncated=stdout_truncated or stderr_truncated,
    )


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
