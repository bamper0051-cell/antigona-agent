from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol


class RiskLevel(IntEnum):
    READ_ONLY = 0
    SAFE_EXECUTION = 1
    WORKSPACE_WRITE = 2
    SYSTEM_CHANGE = 3
    EXTERNAL_IRREVERSIBLE = 4


class ToolStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DENIED = "denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class ToolContext:
    operation_id: str
    workspace: Path
    owner_verified: bool = False
    otp_verified: bool = False
    platform: str = "unknown"
    requester_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def normalized_workspace(self) -> Path:
        return self.workspace.expanduser().resolve()


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: Mapping[str, Any]
    hypothesis: str
    reason: str
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    def fingerprint(self) -> str:
        payload = {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolStatus
    summary: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_type: str | None = None
    retryable: bool = False
    duration_ms: int = 0
    truncated: bool = False
    artifacts: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS


@dataclass(frozen=True)
class ToolCallRecord:
    call: ToolCall
    result: ToolResult


class ToolHandler(Protocol):
    def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult | Awaitable[ToolResult]: ...


AvailabilityCheck = Callable[[ToolContext], bool]
SchemaFactory = Callable[[ToolContext], Mapping[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    toolset: str
    capabilities: frozenset[str]
    risk_level: RiskLevel
    side_effects: bool
    idempotent: bool
    timeout_seconds: float = 60.0
    max_retries: int = 0
    availability_check: AvailabilityCheck | None = None
    dynamic_schema: SchemaFactory | None = None

    def schema_for(self, context: ToolContext) -> Mapping[str, Any]:
        if self.dynamic_schema is None:
            return self.input_schema
        merged = dict(self.input_schema)
        merged.update(dict(self.dynamic_schema(context)))
        return merged
