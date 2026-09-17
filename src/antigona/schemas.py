from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .result_safety import is_sensitive_path, sanitize_result_text

_PUBLIC_TEXT_LIMIT = 512
_SAFE_RESULT_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "completed",
        "done",
        "error",
        "failed",
        "ok",
        "pending",
        "running",
        "timeout",
    }
)
_EXCEPTION_REASON_RE = re.compile(
    r"(?i)(?:^|\s)(?:[a-z_][a-z0-9_.]*(?:error|exception))\s*:"
)


def _public_text(value: object, fallback: str) -> str:
    projected = sanitize_result_text(value, max_length=_PUBLIC_TEXT_LIMIT)
    if not projected:
        return fallback
    return projected


def _public_path(value: object) -> str:
    raw = str(value)
    if is_sensitive_path(raw):
        return "[withheld]"
    return _public_text(raw, "[withheld]")


def _public_reason(value: object) -> str:
    raw = str(value)
    if _EXCEPTION_REASON_RE.search(raw):
        return "state transition"
    return _public_text(raw, "state transition")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _projection_digest(value: object) -> str:
    if isinstance(value, dict) and _is_sha256(value.get("arguments_sha256")):
        return str(value["arguments_sha256"]).casefold()
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _public_step_input(value: object) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    if isinstance(value, dict):
        tool_name = value.get("tool_name")
        if tool_name in {"workspace.write_text", "sandbox.shell"}:
            projection["tool_name"] = tool_name
    projection["arguments_sha256"] = _projection_digest(value)
    return projection


def _public_result_metadata(value: object, *, nested: bool = True) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    projection: dict[str, Any] = {}
    for key in ("ok", "blocked", "text_omitted", "verified", "retryable"):
        item = value.get(key)
        if isinstance(item, bool):
            projection[key] = item
    status = value.get("status")
    if isinstance(status, str) and status.casefold() in _SAFE_RESULT_STATUSES:
        projection["status"] = status
    for key in ("exit_code", "size", "bytes_written", "artifact_count", "retries"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            projection[key] = item
    sha256 = value.get("sha256")
    if _is_sha256(sha256):
        projection["sha256"] = str(sha256).casefold()
    if nested:
        tool_result = _public_result_metadata(value.get("tool_result"), nested=False)
        if tool_result:
            projection["tool_result"] = tool_result
    return projection or None


class TaskCreate(BaseModel):
    goal:str=Field(min_length=1); path:str=Field(min_length=1); content:str
    tool_name:Literal["workspace.write_text","workspace.read_text","sandbox.shell","mcp","send_email"]="workspace.write_text"
    command:list[str]=Field(default_factory=list)
    read_after_write:bool=Field(default=False)
    run_after_write:bool=Field(default=False,description="B5: run run_command after the write step")
    run_command:list[str]=Field(default_factory=list,description="B5: argv executed after the write step")
    fix_after_run:bool=Field(default=False,description="L7-1: fix and rerun after initial run")
    fix_content:str=Field(default="",description="L7-1: fixed content for step 2")
    fix_command:list[str]=Field(default_factory=list,description="L7-1: rerun command for step 3")
    mcp_server:str=Field(default="",description="MCP server name (required when tool_name=mcp)")
    mcp_tool:str=Field(default="",description="MCP tool name to invoke (required when tool_name=mcp)")
    mcp_arguments:dict[str,Any]=Field(default_factory=dict,description="Arguments for the MCP tool (when tool_name=mcp)")
    params:dict[str,Any]=Field(default_factory=dict,description="Generic tool params (e.g. send_email: to/subject/body/attachment)")
class TaskSubmit(BaseModel):
    message:str=Field(min_length=1,description="Free-text task description")
    conversation_id:str=Field(default="",description="Optional conversation/session identifier")
    client:str=Field(default="cli",description="Client identifier (cli, telegram, dashboard)")
    metadata:dict[str,Any]=Field(default_factory=dict,description="Arbitrary client metadata")

# Canonical dialogue turn contract (Stage 1: single server core). These schemas
# live here so BOTH FastAPI applications (canonical gateway and legacy api.server)
# share one contract, and only the canonical Gateway owns the endpoint.
class DialogueTurnRequest(BaseModel):
    text: str = Field(min_length=1)
    session_id: str
    channel: str = "cli"
    user_id: str = "default"
    turn_id: str = ""



class DialogueTurnResponse(BaseModel):
    reply: str
    session_id: str
    # Verified semantics (Stage 1 correction): an ordinary LLM reply,
    # clarification, or task_accepted is NOT a verified TaskFlow result.
    # Only a final Verifier-confirmed task result may set task_verified=True.
    # verified=None means "not applicable / not yet verified".
    verified: bool | None = None
    response_verified: bool = False
    task_verified: bool = False
    # Canonical brain routing result — thin clients use this to decide how to
    # render the turn without running any local DialogueEngine/IntentRouter.
    response_type: str = "conversation"
    flow_id: str | None = None
    requires_approval: bool = False
    # Truth contract (turn outcome): the REAL tool/step outcome of this turn.
    # ``None`` means the turn did not run a tool (conversation / clarification /
    # control / task acceptance).  A client must never render a success header
    # for a turn whose ``tool_outcome`` is non-SUCCEEDED.
    # Values: "SUCCEEDED" | "PARTIAL" | "FAILED" | "DENIED".
    tool_outcome: str | None = None
    # Human-auditable error detail for a failed/denied/partial turn.  This is
    # an owner-scoped audit field; a client MUST sanitize internal security
    # wording before it reaches the user-visible chat text.
    last_error: str | None = None


class ApprovalDecision(BaseModel): approve:bool
class StepView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; index:int; title:str; status:str; input:dict[str,Any]; output:dict[str,Any]|None; retries:int

    @field_validator("title", mode="before")
    @classmethod
    def project_title(cls, value: object) -> str:
        return _public_text(value, "flow step")

    @field_validator("input", mode="before")
    @classmethod
    def project_input(cls, value: object) -> dict[str, Any]:
        return _public_step_input(value)

    @field_validator("output", mode="before")
    @classmethod
    def project_output(cls, value: object) -> dict[str, Any] | None:
        return _public_result_metadata(value)


class TransitionView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    entity_id:str; entity_type:str; from_state:str|None; to_state:str; reason:str; actor:str; correlation_id:str; created_at:datetime

    @field_validator("reason", mode="before")
    @classmethod
    def project_reason(cls, value: object) -> str:
        return _public_reason(value)


class ArtifactView(BaseModel):
    """Artifact metadata exposed by the generic flow endpoint.

    Verifier evidence can contain read-back content, so it is intentionally not
    part of this broad status projection.  The owner-scoped result endpoint is
    the only API that may expose a sanitized verifier-approved read-back.
    """

    model_config = ConfigDict(from_attributes=True)
    id: str
    step_id: str
    path: str
    sha256: str
    size: int
    verified: bool

    @field_validator("path", mode="before")
    @classmethod
    def project_path(cls, value: object) -> str:
        return _public_path(value)


class VerifiedArtifactResultView(BaseModel):
    path: str
    sha256: str
    size: int
    verified: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def project_path(cls, value: object) -> str:
        return _public_path(value)


class FlowResultView(BaseModel):
    flow_id: str
    status: str
    terminal: bool
    success: bool
    artifacts: list[VerifiedArtifactResultView] = Field(default_factory=list)
    safe_result_text: str | None = None
    stdout_preview: str | None = None
    failure_reason: str | None = None
    completed_at: datetime | None = None
    revision: int


class ApprovalView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; tool_name:str; risk_level:str; reason:str; decision:str; decided_by:str|None

    @field_validator("reason", mode="before")
    @classmethod
    def project_reason(cls, _value: object) -> str:
        return "approval required"


class TaskView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; goal:str; target_path:str; status:str; revision:int; checkpoint:str; cancellation_requested:bool; created_at:datetime; updated_at:datetime
    correlation_id:str|None=None
    steps:list[StepView]; transitions:list[TransitionView]; artifacts:list[ArtifactView]; approvals:list[ApprovalView]

    @field_validator("goal", mode="before")
    @classmethod
    def project_goal(cls, value: object) -> str:
        return _public_text(value, "flow")

    @field_validator("target_path", mode="before")
    @classmethod
    def project_target_path(cls, value: object) -> str:
        return _public_path(value)


class EventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: str = "transition"
    flow_id: str
    correlation_id: str
    from_state: str | None = None
    to_state: str
    reason: str
    actor: str
    timestamp: str
    seq: int

    @field_validator("reason", mode="before")
    @classmethod
    def project_reason(cls, value: object) -> str:
        return _public_reason(value)


# ── Cron schedule schemas ────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron_expression: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1)
    target_path: str = "workspace"
    content: str = ""
    tool_name: str = "workspace.write_text"
    tool_arguments: dict[str, Any] = Field(default_factory=dict)


class ScheduleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    cron_expression: str
    owner_id: str
    goal: str
    target_path: str
    content: str
    tool_name: str
    tool_arguments: dict[str, Any]
    enabled: bool
    cancelled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime



class ScheduleEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    schedule_id: str
    event_type: str
    message: str | None
    correlation_id: str
    created_at: datetime


class ScheduleJobView(BaseModel):
    id: str
    goal: str
    status: str
    created_at: datetime


# ── Replay schemas (P2.3) ────────────────────────────────────────────
#
# These mirror the dataclasses in ``antigona.replay`` one-for-one. The engine
# stays the single source of truth for the projection; these models exist only
# so the Gateway returns a typed, documented body instead of a raw dict.


class ReplayTransitionView(BaseModel):
    id: int
    entity_id: str
    entity_type: str
    from_state: str | None
    to_state: str
    reason: str
    actor: str
    created_at: str

    @field_validator("reason", mode="before")
    @classmethod
    def project_reason(cls, value: object) -> str:
        return _public_reason(value)


class ReplayStepView(BaseModel):
    id: str
    index: int
    title: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    retries: int

    @field_validator("title", mode="before")
    @classmethod
    def project_title(cls, value: object) -> str:
        return _public_text(value, "flow step")

    @field_validator("input", mode="before")
    @classmethod
    def project_input(cls, value: object) -> dict[str, Any]:
        return _public_step_input(value)

    @field_validator("output", mode="before")
    @classmethod
    def project_output(cls, value: object) -> dict[str, Any] | None:
        return _public_result_metadata(value)


class ReplayArtifactView(BaseModel):
    id: str
    step_id: str
    path: str
    sha256: str
    size: int
    verified: bool

    @field_validator("path", mode="before")
    @classmethod
    def project_path(cls, value: object) -> str:
        return _public_path(value)


class ReplayResponse(BaseModel):
    task_id: str
    owner_id: str
    goal: str
    target_path: str
    status: str
    revision: int
    created_at: str
    updated_at: str
    steps: list[ReplayStepView] = Field(default_factory=list)
    transitions: list[ReplayTransitionView] = Field(default_factory=list)
    artifacts: list[ReplayArtifactView] = Field(default_factory=list)

    @field_validator("goal", mode="before")
    @classmethod
    def project_goal(cls, value: object) -> str:
        return _public_text(value, "flow")

    @field_validator("target_path", mode="before")
    @classmethod
    def project_target_path(cls, value: object) -> str:
        return _public_path(value)


class TimelineEntry(BaseModel):
    type: str  # transition | step | rejected | artifact
    timestamp: str
    entity_id: str
    description: str
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def project_public_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        entry_type = str(data.get("type") or "")
        details = data.get("details")
        safe_details: dict[str, Any] = {}
        if isinstance(details, dict):
            for key in ("actor", "entity_type", "step_id"):
                item = details.get(key)
                if isinstance(item, str):
                    safe_details[key] = _public_text(item, "unknown")
            verified = details.get("verified")
            if isinstance(verified, bool):
                safe_details["verified"] = verified
        data["details"] = safe_details
        if entry_type == "artifact":
            data["description"] = "artifact recorded"
        else:
            data["description"] = _public_text(
                data.get("description"), "state transition"
            )
        return data


class TimelineResponse(BaseModel):
    task_id: str
    entries: list[TimelineEntry] = Field(default_factory=list)


# ── List views (P2.4, TUI) ───────────────────────────────────────────
#
# The TUI needs owner-scoped *collections* that the P0 API never exposed:
# it renders tables, not single flows. These stay deliberately narrow —
# a row's worth of columns each — so listing never leaks step payloads,
# tool arguments or content of somebody else's work.


class FlowSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    goal: str
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime

    @field_validator("goal", mode="before")
    @classmethod
    def project_goal(cls, value: object) -> str:
        return _public_text(value, "flow")


class FlowListView(BaseModel):
    items: list[FlowSummary] = Field(default_factory=list)
    total: int = 0


class ApprovalListEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    task_id: str
    tool_name: str
    risk_level: str
    reason: str
    created_at: datetime

    @field_validator("reason", mode="before")
    @classmethod
    def project_reason(cls, _value: object) -> str:
        return "approval required"


class ApprovalListView(BaseModel):
    items: list[ApprovalListEntry] = Field(default_factory=list)
    total: int = 0


class SteerFlowRequest(BaseModel):
    message: str = Field(min_length=1)

