from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    # Naive UTC. SQLite (pysqlite) does not preserve tzinfo on DateTime(timezone=True)
    # columns: values are persisted and reloaded as naive. A tz-aware value written by
    # the ORM would be compared against naive values loaded from the DB during SQLAlchemy
    # post-sync evaluation, raising TypeError. Storing naive UTC keeps all comparisons
    # homogeneous. Use datetime.now(UTC) only for log timestamps where tz awareness is wanted.
    return datetime.now(UTC).replace(tzinfo=None)


class TaskState(StrEnum):
    RECEIVED="RECEIVED"; QUEUED="QUEUED"; PLANNING="PLANNING"; RUNNING="RUNNING"; TOOL_EXECUTING="TOOL_EXECUTING"; OBSERVING="OBSERVING"; VERIFYING="VERIFYING"; WAITING_APPROVAL="WAITING_APPROVAL"; DONE="DONE"; FAILED="FAILED"; BLOCKED="BLOCKED"; CANCELLED="CANCELLED"; TIMEOUT="TIMEOUT"; POLICY_DENIED="POLICY_DENIED"; CREATED="CREATED"; READY="READY"; RETRY_SCHEDULED="RETRY_SCHEDULED"; REPLAN_REQUESTED="REPLAN_REQUESTED"; WAITING_USER="WAITING_USER"; PAUSED="PAUSED"


class StepState(StrEnum):
    PENDING="PENDING"; RUNNING="RUNNING"; COMPLETED="COMPLETED"; CANCELLED="CANCELLED"; FAILED="FAILED"


class TaskFlow(Base):
    __tablename__="task_flows"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4()))
    owner_id:Mapped[str]=mapped_column(String(128),index=True); goal:Mapped[str]=mapped_column(Text)
    target_path:Mapped[str]=mapped_column(Text); content:Mapped[str]=mapped_column(Text,default=""); payload_fingerprint:Mapped[str]=mapped_column(String(64),default="")
    tool_name:Mapped[str]=mapped_column(String(64),default="workspace.write_text"); tool_arguments:Mapped[dict[str,Any]]=mapped_column(JSON,default=dict)
    status:Mapped[str]=mapped_column(String(32),default=TaskState.RECEIVED.value); revision:Mapped[int]=mapped_column(Integer,default=0)
    cancellation_requested:Mapped[bool]=mapped_column(Boolean,default=False); idempotency_key:Mapped[str]=mapped_column(String(255),default=""); checkpoint:Mapped[str]=mapped_column(String(64),default="accepted")
    side_effect_key:Mapped[str|None]=mapped_column(String(64),nullable=True); lease_owner:Mapped[str|None]=mapped_column(String(128),nullable=True)
    lease_expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); heartbeat_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    parent_id:Mapped[str|None]=mapped_column(ForeignKey("task_flows.id",ondelete="CASCADE"),nullable=True,index=True)
    depth:Mapped[int]=mapped_column(Integer,default=0); max_depth:Mapped[int]=mapped_column(Integer,default=3); max_child_budget:Mapped[int]=mapped_column(Integer,default=5)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint("owner_id","idempotency_key",name="uq_owner_idem"),)
    steps:Mapped[list[FlowStep]]=relationship(back_populates="task",cascade="all, delete-orphan"); transitions:Mapped[list[StateTransition]]=relationship(back_populates="task",cascade="all, delete-orphan")
    artifacts:Mapped[list[Artifact]]=relationship(back_populates="task",cascade="all, delete-orphan"); approvals:Mapped[list[Approval]]=relationship(back_populates="task",cascade="all, delete-orphan")
    parent:Mapped[TaskFlow|None]=relationship("TaskFlow",remote_side=[id],back_populates="children")
    children:Mapped[list[TaskFlow]]=relationship("TaskFlow",back_populates="parent",cascade="all, delete-orphan")

    @property
    def correlation_id(self) -> str | None:
        return self.transitions[0].correlation_id if self.transitions else None


class FlowStep(Base):
    __tablename__="flow_steps"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True)
    index:Mapped[int]=mapped_column(Integer,default=0); title:Mapped[str]=mapped_column(String(255),default="")
    step_number:Mapped[int]=mapped_column(Integer,default=0); tool_name:Mapped[str]=mapped_column(String(128),default=""); arguments:Mapped[dict[str,Any]]=mapped_column(JSON,default=dict)
    status:Mapped[str]=mapped_column(String(32),default=StepState.PENDING.value)
    revision:Mapped[int]=mapped_column(Integer,default=0); input:Mapped[dict[str,Any]]=mapped_column(JSON,default=dict); output:Mapped[dict[str,Any]|None]=mapped_column(JSON,nullable=True); retries:Mapped[int]=mapped_column(Integer,default=0)
    task:Mapped[TaskFlow]=relationship(back_populates="steps")
    artifacts:Mapped[list[Artifact]]=relationship(back_populates="step")


class StateTransition(Base):
    __tablename__="state_transitions"
    id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True); entity_id:Mapped[str]=mapped_column(String(36)); entity_type:Mapped[str]=mapped_column(String(16))
    from_state:Mapped[str|None]=mapped_column(String(32),nullable=True); to_state:Mapped[str]=mapped_column(String(32)); reason:Mapped[str]=mapped_column(Text); actor:Mapped[str]=mapped_column(String(64)); correlation_id:Mapped[str]=mapped_column(String(36)); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    task:Mapped[TaskFlow]=relationship(back_populates="transitions")

    @property
    def seq(self) -> int:
        return self.id


class Artifact(Base):
    __tablename__="artifacts"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True); step_id:Mapped[str]=mapped_column(ForeignKey("flow_steps.id")); path:Mapped[str]=mapped_column(Text); sha256:Mapped[str]=mapped_column(String(64)); size:Mapped[int]=mapped_column(Integer); verified:Mapped[bool]=mapped_column(Boolean,default=False); evidence:Mapped[dict[str,Any]]=mapped_column(JSON,default=dict); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    task:Mapped[TaskFlow]=relationship(back_populates="artifacts")
    step:Mapped[FlowStep]=relationship(back_populates="artifacts")


class Approval(Base):
    __tablename__="approvals"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True); tool_name:Mapped[str]=mapped_column(String(128)); arguments:Mapped[dict[str,Any]]=mapped_column(JSON); risk_level:Mapped[str]=mapped_column(String(16)); reason:Mapped[str]=mapped_column(Text); decision:Mapped[str]=mapped_column(String(16),default="PENDING"); decided_by:Mapped[str|None]=mapped_column(String(128),nullable=True); decided_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    # 7B: SHA256 token hash of one-shot ApprovalGrantStore grant minted on owner approval.
    # Deliberately a column and NOT part of ``arguments``: ``arguments`` is
    # serialized into every ApprovalView the API returns, this must not be.
    grant_token:Mapped[str|None]=mapped_column(String(128),nullable=True)
    task:Mapped[TaskFlow]=relationship(back_populates="approvals")


class QueueJob(Base):
    __tablename__="queue_jobs"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),unique=True,index=True); lane:Mapped[str]=mapped_column(String(32),default="main"); status:Mapped[str]=mapped_column(String(16),default="QUEUED",index=True); attempts:Mapped[int]=mapped_column(Integer,default=0); available_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); lease_owner:Mapped[str|None]=mapped_column(String(128),nullable=True); lease_expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); heartbeat_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); last_error:Mapped[str|None]=mapped_column(Text,nullable=True); correlation_id:Mapped[str]=mapped_column(String(36)); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class DurableOperation(Base):
    __tablename__="durable_operations"
    id:Mapped[str]=mapped_column(String(64),primary_key=True); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True); step_id:Mapped[str]=mapped_column(ForeignKey("flow_steps.id")); kind:Mapped[str]=mapped_column(String(64)); status:Mapped[str]=mapped_column(String(16),default="PREPARED"); request:Mapped[dict[str,Any]]=mapped_column(JSON); result:Mapped[dict[str,Any]|None]=mapped_column(JSON,nullable=True); attempts:Mapped[int]=mapped_column(Integer,default=0); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class DeliveryOutbox(Base):
    __tablename__="delivery_outbox"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); task_id:Mapped[str]=mapped_column(ForeignKey("task_flows.id"),index=True); adapter:Mapped[str]=mapped_column(String(32)); event_type:Mapped[str]=mapped_column(String(32)); payload:Mapped[dict[str,Any]]=mapped_column(JSON); idempotency_key:Mapped[str]=mapped_column(String(128),unique=True); status:Mapped[str]=mapped_column(String(16),default="PENDING",index=True); attempts:Mapped[int]=mapped_column(Integer,default=0); available_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); lease_owner:Mapped[str|None]=mapped_column(String(128),nullable=True); lease_expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); delivered_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); last_error:Mapped[str|None]=mapped_column(Text,nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class DeliveryReceipt(Base):
    __tablename__ = "delivery_receipts"
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    adapter: Mapped[str] = mapped_column(String(32), default="")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: Transmission-level flag: True means the message left the process toward the
    #: external channel. It is NOT confirmation of delivery to the recipient and
    #: must never be read as such (see ``antigona.delivery.readback``).
    transmitted: Mapped[bool] = mapped_column(Boolean, default=True)
    #: B53 (DELIV-02): provider-level confirmation. ``provider_message_id`` holds
    #: the identifier the provider returned for the message (e.g. Telegram
    #: ``message_id``). ``read_back_status`` is one of SEND_ACK / UNSUPPORTED /
    #: REFUTED (see ``antigona.delivery.readback``): SEND_ACK confirms *transmission*
    #: only, never that a human read the message; UNSUPPORTED is never a delivery
    #: claim. ``read_back_at`` records when the acknowledgement was obtained. All
    #: three are nullable so pre-B53 rows stay valid without a backfill.
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read_back_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    read_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceRecord(Base):
    __tablename__="evidence_registry"
    evidence_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    task_id:Mapped[str]=mapped_column(String(36),index=True)
    attempt_id:Mapped[str]=mapped_column(String(128),index=True)
    type:Mapped[str]=mapped_column(String(64))
    kind:Mapped[str]=mapped_column(String(64),default="GENERIC",index=True)
    outcome:Mapped[str]=mapped_column(String(32),default="",index=True)
    correlation_id:Mapped[str]=mapped_column(String(64),default="",index=True)
    source:Mapped[str]=mapped_column(String(32),index=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,index=True)
    sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    artifact_reference:Mapped[str|None]=mapped_column(Text,nullable=True)
    status:Mapped[str]=mapped_column(String(16),default="PROPOSED",index=True)
    supports_claims:Mapped[list[str]]=mapped_column(JSON,default=list)
    verified_by:Mapped[str|None]=mapped_column(String(128),nullable=True)


def skill_slug(name: str) -> str:
    """Derive an ASKILL/1 ``slug`` from a skill name, deterministically.

    The grammar (docs/SKILL_FORMAT.md §3) allows lowercase alphanumerics and ``-``
    only, 1..64 bytes; every other run of characters collapses into a single dash.
    Returns an empty string when nothing survives — callers supply the fallback.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].rstrip("-")


def _default_slug(context: DefaultExecutionContext) -> str:
    # Column-level default so a bare Skill(name=...) — as in tests/unit/test_skills.py —
    # never hits a NOT NULL violation, whichever path inserts the row.
    parameters: dict[str, Any] = context.get_current_parameters()  # type: ignore[no-untyped-call]
    fallback = "skill-" + str(parameters.get("id") or "").lower().replace("-", "")
    return skill_slug(str(parameters.get("name") or "")) or fallback[:64]


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), index=True)
    # Denormalized P0 fields, kept for backward compatibility with SkillsRegistry callers.
    trigger: Mapped[str] = mapped_column(Text)
    trajectory_ref: Mapped[str] = mapped_column(String(128), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True, default="", server_default="")
    slug: Mapped[str] = mapped_column(String(64), index=True, default=_default_slug, server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", server_default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    format_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    body_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    body_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    body_path: Mapped[str] = mapped_column(Text, default="", server_default="")
    trust: Mapped[str] = mapped_column(String(16), default="trusted", server_default="trusted")
    risk_ceiling: Mapped[str] = mapped_column(String(16), default="MEDIUM", server_default="MEDIUM")
    source_flow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("owner_id", "slug", "version", name="uq_skill_owner_slug_version"),)


class SkillTransition(Base):
    """Append-only journal of skill lifecycle attempts, mirroring ``state_transitions``.

    Refused transitions are recorded too (``accepted=False``) so a rejected promotion
    leaves the same audit trail as an accepted one.
    """

    __tablename__ = "skill_transitions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    skill_id: Mapped[str] = mapped_column(String(36), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CronSchedule(Base):
    __tablename__ = "cron_schedules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), index=True)
    cron_expression: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    goal: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text, default="workspace")
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str] = mapped_column(String(64), default="workspace.write_text")
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TelegramMessageBinding(Base):
    """Persistent binding between a Telegram message and a Gateway flow/task.

    Enables reliable context resolution via ``chat_id + telegram_message_id``
    regardless of edit, reply, or status messages.  One row per unique
    (chat_id, telegram_message_id) pair.

    Attributes:
        id: UUID primary key.
        chat_id: Telegram chat (conversation) ID.
        telegram_message_id: Telegram message ID within the chat.
        user_id: Telegram user who sent the message (0 for system/bot messages).
        task_id: UUID of the Gateway flow / task this message belongs to.
        session_id: Optional conversation session ID.
        step_id: Optional workflow step ID.
        correlation_id: End-to-end trace ID from the input envelope.
        message_role: Role — ``user``, ``assistant``, ``system``, ``status``.
        message_kind: Semantic kind — ``text``, ``voice``, ``transcription``,
            ``task_created``, ``task_progress``, ``task_result``,
            ``question``, ``approval_request``, ``error``, ``artifact``.
        source_message_id: If this message is a reply, the original message ID.
        original_text: The text as originally received / sent.
        edited_text: If edited — the latest text.
        edit_version: Monotonically increasing edit counter.
        metadata_json: Arbitrary JSON metadata.
    """

    __tablename__ = "telegram_message_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_role: Mapped[str] = mapped_column(String(16), default="user")
    message_kind: Mapped[str] = mapped_column(String(32), default="text")
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_version: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_telegram_msg_binding"),
    )


class ScheduleEvent(Base):
    """Append-only journal of cron schedule lifecycle events."""

    __tablename__ = "schedule_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(16))  # created|ticked|errored|cancelled
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryEntry(Base):
    """Единая память Antigona (Step 5-6 манифеста).

    Одна таблица памяти в основной БД (antigona.db). Заменяет разрозненные
    FileMemory (MEMORY.md/USER.md), LongTermMemory (antigona_memory.db),
    SelfLearningTool (learnings.json). Только ядро (Gateway) пишет сюда;
    интерфейсы (CLI/Telegram) работают через API.

    kind: fact | preference | profile
    """

    __tablename__ = "memory_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    kind: Mapped[str] = mapped_column(String(16), default="fact", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="user")  # user|task|core
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
