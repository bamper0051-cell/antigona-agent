from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .contracts import RiskLevel, ToolCall, ToolCallRecord, ToolContext, ToolSpec


_DANGEROUS_COMMAND_PATTERNS = (
    re.compile(r"(^|\s)rm\s+-rf\s+/(\s|$)"),
    re.compile(r"(^|\s)mkfs(\.|\s)"),
    re.compile(r"(^|\s)shutdown(\s|$)"),
    re.compile(r"(^|\s)reboot(\s|$)"),
    re.compile(r"(^|\s)systemctl\s+(stop|disable)\s+"),
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str


class ToolPolicyEngine:
    def authorize(
        self,
        spec: ToolSpec,
        call: ToolCall,
        context: ToolContext,
        history: Sequence[ToolCallRecord],
    ) -> PolicyDecision:
        if not call.hypothesis.strip():
            return PolicyDecision(False, "MISSING_HYPOTHESIS", "Tool call must test a hypothesis.")
        if not call.reason.strip():
            return PolicyDecision(False, "MISSING_REASON", "Tool selection reason is required.")

        fingerprint = call.fingerprint()
        for record in reversed(history):
            if record.call.fingerprint() == fingerprint:
                return PolicyDecision(
                    False,
                    "SEMANTIC_DUPLICATE",
                    f"Equivalent call already executed: {record.call.call_id}",
                )

        if spec.risk_level >= RiskLevel.SYSTEM_CHANGE and not context.owner_verified:
            return PolicyDecision(False, "OWNER_REQUIRED", "Owner identity verification is required.")

        if spec.risk_level >= RiskLevel.EXTERNAL_IRREVERSIBLE and not context.otp_verified:
            return PolicyDecision(False, "OTP_REQUIRED", "OTP/TOTP confirmation is required.")

        if spec.name == "terminal":
            command = call.arguments.get("command", [])
            rendered = " ".join(command) if isinstance(command, (list, tuple)) else str(command)
            for pattern in _DANGEROUS_COMMAND_PATTERNS:
                if pattern.search(rendered):
                    return PolicyDecision(False, "DANGEROUS_COMMAND", "Command is blocked by policy.")

        return PolicyDecision(True, "ALLOW", "Allowed")
