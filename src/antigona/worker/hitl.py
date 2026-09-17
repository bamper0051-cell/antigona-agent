from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from antigona.core.paths import home_dir
from antigona.models import utcnow
from antigona.security.risk_classifier import (
    is_high_risk_path,
    is_safe_workspace_path,
    is_sensitive_write_target,
    is_write_action,
)


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfirmationPolicyMode(StrEnum):
    NEVER = "NEVER"
    HIGH_ONLY = "HIGH_ONLY"
    ALWAYS = "ALWAYS"


@dataclass
class ConfirmationPolicy:
    mode: ConfirmationPolicyMode = ConfirmationPolicyMode.ALWAYS
    timeout_seconds: float = 60.0

    def should_require_approval(self, risk_level: RiskLevel | str) -> bool:
        rl = str(risk_level).upper()
        if self.mode == ConfirmationPolicyMode.NEVER:
            return False
        if self.mode == ConfirmationPolicyMode.HIGH_ONLY:
            return rl == RiskLevel.HIGH.value
        if self.mode == ConfirmationPolicyMode.ALWAYS:
            return rl in (RiskLevel.MEDIUM.value, RiskLevel.HIGH.value)
        return True

    def should_auto_approve(self, risk_level: RiskLevel | str) -> bool:
        return not self.should_require_approval(risk_level)


_GLOBAL_POLICY: ConfirmationPolicy = ConfirmationPolicy()


def set_confirmation_policy(
    policy: ConfirmationPolicy | ConfirmationPolicyMode | str,
    timeout_seconds: float | None = None,
) -> ConfirmationPolicy:
    """Configure the active confirmation policy."""
    global _GLOBAL_POLICY
    if isinstance(policy, ConfirmationPolicy):
        if timeout_seconds is not None:
            policy.timeout_seconds = timeout_seconds
        _GLOBAL_POLICY = policy
    elif isinstance(policy, (ConfirmationPolicyMode, str)):
        mode_enum = ConfirmationPolicyMode(str(policy).upper())
        to = timeout_seconds if timeout_seconds is not None else _GLOBAL_POLICY.timeout_seconds
        _GLOBAL_POLICY = ConfirmationPolicy(mode=mode_enum, timeout_seconds=to)
    else:
        raise TypeError(f"Invalid policy type: {type(policy)}")
    return _GLOBAL_POLICY


def get_confirmation_policy() -> ConfirmationPolicy:
    """Return the active confirmation policy."""
    return _GLOBAL_POLICY


DESTRUCTIVE_COMMAND_RE = re.compile(
    r"\b(rm|rmdir|sudo|su|chmod|chown|dd|mkfs|fdisk|parted|kill|pkill|shutdown|reboot|systemctl)\b",
    re.IGNORECASE,
)
NETWORK_COMMAND_RE = re.compile(
    r"\b(curl|wget|nc|netcat|nmap|ssh|scp|ftp|ping|nslookup|dig|telnet)\b",
    re.IGNORECASE,
)
SENSITIVE_PATH_RE = re.compile(
    r"(\.\./|/etc/|/usr/|/var/|" + re.escape(str(home_dir())) + r"/|/sys/|/proc/|\.ssh|\.env)",
    re.IGNORECASE,
)

# Read-only / diagnostic commands that carry no meaningful risk of side
# effects (equivalent in risk to a plain file read). Deliberately small and
# conservative: a command only downgrades to LOW when its first token is one
# of these AND the command contains no redirection (`>`, `>>`, `<`) or pipe
# (`|`) that could turn a read into a write. Everything else — writes,
# installs, unknown binaries, piped mutations — still falls through to
# MEDIUM and is gated by the confirmation policy like before.
READONLY_SHELL_COMMANDS = frozenset(
    {
        "ls", "pwd", "uptime", "whoami", "date", "uname", "echo", "printf",
        "cat", "head", "tail", "wc", "free", "hostname", "id", "env",
        "which", "stat", "file", "nproc", "lscpu", "lsblk", "w", "who",
        "last", "ps", "top", "df", "du", "printenv",
    }
)


def evaluate_risk(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    target_path: str | None = None,
    workspace_root: str | Path | None = None,
) -> tuple[RiskLevel, str]:
    """Lightweight risk classifier using tool heuristics.

    Returns (RiskLevel, reasoning_string).
    """
    args = arguments or {}
    path = target_path or str(args.get("path", ""))

    # 1. External network access tools
    if tool_name in ("web.fetch", "web_fetch", "network.http"):
        return RiskLevel.HIGH, f"External network access requested via {tool_name}"

    # 2. Shell execution
    if tool_name in ("sandbox.shell", "shell"):
        raw_cmd = args.get("command", [])
        if isinstance(raw_cmd, (list, tuple)):
            cmd_str = " ".join(str(x) for x in raw_cmd)
        else:
            cmd_str = str(raw_cmd)

        if DESTRUCTIVE_COMMAND_RE.search(cmd_str):
            return RiskLevel.HIGH, f"Destructive/system shell command detected: {cmd_str}"
        if NETWORK_COMMAND_RE.search(cmd_str):
            return RiskLevel.HIGH, f"Network exfiltration shell command detected: {cmd_str}"
        if SENSITIVE_PATH_RE.search(cmd_str):
            return RiskLevel.HIGH, f"Shell command references sensitive/external path: {cmd_str}"

        stripped_cmd = cmd_str.strip()
        first_token = stripped_cmd.split(" ", 1)[0].rsplit("/", 1)[-1] if stripped_cmd else ""
        has_redirection = any(ch in cmd_str for ch in ("|", ">", "<"))
        if first_token in READONLY_SHELL_COMMANDS and not has_redirection:
            return RiskLevel.LOW, f"Read-only/diagnostic shell command: {cmd_str}"
        return RiskLevel.MEDIUM, f"Shell command execution: {cmd_str}"

    # 3. File write operations
    if tool_name in ("workspace.write_text", "file.write") or is_write_action(tool_name):
        if is_safe_workspace_path(path, workspace_root=workspace_root):
            return RiskLevel.LOW, f"Workspace file modification: {path}"
        if (
            SENSITIVE_PATH_RE.search(path)
            or is_high_risk_path(path, workspace_root=workspace_root)
            or is_sensitive_write_target(path)
        ):
            return RiskLevel.HIGH, f"File write to sensitive/external path: {path}"
        return RiskLevel.MEDIUM, f"Workspace file modification: {path}"

    # 4. File read operations
    if tool_name in ("workspace.read_text", "file.read"):
        if SENSITIVE_PATH_RE.search(path):
            return RiskLevel.HIGH, f"Read sensitive path: {path}"
        return RiskLevel.LOW, f"Workspace file read: {path}"

    # 4b. Send file (outbound delivery). Secret files are HIGH (require
    # explicit owner confirmation); normal files MEDIUM (approval/PIN gate).
    if tool_name in ("send_file", "SEND_FILE", "file.send"):
        _secret_ext = (".pem", ".key", ".p12", ".pfx", ".crt", ".env", ".jks", ".json")
        lower = path.lower()
        if any(lower.endswith(ext) for ext in _secret_ext):
            return RiskLevel.HIGH, f"Send secret file (exfil risk): {path}"
        return RiskLevel.MEDIUM, f"Send file outbound: {path}"

    # Default fallback for unknown tools
    return RiskLevel.MEDIUM, f"Unknown tool execution: {tool_name}"


class SecurityAnalyzerWrapper:
    """SecurityAnalyzer interface wrapper with automatic fallback."""

    def __init__(self, sdk_analyzer: Any | None = None) -> None:
        self.sdk_analyzer = sdk_analyzer

    def analyze(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        target_path: str | None = None,
    ) -> tuple[RiskLevel, str]:
        if self.sdk_analyzer is not None:
            try:
                res = self.sdk_analyzer.analyze(tool_name=tool_name, arguments=arguments)
                if hasattr(res, "risk_level"):
                    return (
                        RiskLevel(str(res.risk_level).upper()),
                        getattr(res, "reason", "SDK risk analysis"),
                    )
            except Exception:
                pass
        return evaluate_risk(tool_name, arguments, target_path)


def check_approval_timeout(approval: Any, timeout_seconds: float | None = None) -> bool:
    """Check if a PENDING approval has exceeded its timeout duration."""
    if not approval or str(getattr(approval, "decision", "")).upper() != "PENDING":
        return False

    created_at = getattr(approval, "created_at", None)
    if created_at is None:
        return False

    if created_at.tzinfo is not None:
        created_at = created_at.replace(tzinfo=None)

    policy = get_confirmation_policy()
    limit = timeout_seconds if timeout_seconds is not None else policy.timeout_seconds

    now = utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    elapsed = float((now - created_at).total_seconds())
    return bool(elapsed >= limit)
