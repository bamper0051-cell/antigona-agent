from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .durable.state_cache import StateCache
from .durable.state_machine import (
    TERMINAL_STATES as TERMINAL,
)
from .durable.state_machine import (
    ConcurrentUpdate,
    InvalidTransition,
    check_step_transition,
    check_task_transition,
    record_rejection,
)
from .models import (
    Approval,
    DeliveryOutbox,
    FlowStep,
    StateTransition,
    StepState,
    TaskFlow,
    TaskState,
    utcnow,
)
from .result_safety import is_sensitive_command, is_sensitive_path, sanitize_result_text

#: An owner approval stays usable for one hour — long enough for the flow to be
#: picked up by a worker, short enough that a stale decision cannot authorize an
#: execution days later. The grant is one-shot regardless.
APPROVAL_GRANT_TTL_SECONDS = 3600

_WRITE_RUN_INTERPRETERS = frozenset(
    # B5 (live round 6): interpreters the system itself puts in front of a
    # just-created in-workspace script for the deterministic write→run compound.
    {"python", "python3", "sh", "bash", "node", "ruby", "perl", "php", "lua"}
)
_SHELL_OPERATOR_CHARS = ("|", ">", "<", ";", "&", "`", "$(", "\n")


def _is_deterministic_write_run_command(command: object, target_path: str | None) -> bool:
    """True only for the system-built run step of a ``file_write_run`` compound.

    Provably safe means: `<interpreter> <created-in-workspace-script> [args]`,
    no shell operators, no destructive/network/sensitive tokens, and the script
    argument is exactly the file the write step just created. Anything else —
    a free-form user shell command, pipes/redirects, another path — returns
    False and keeps the normal approval gate.
    """
    from antigona.worker.hitl import (
        DESTRUCTIVE_COMMAND_RE,
        NETWORK_COMMAND_RE,
        SENSITIVE_PATH_RE,
    )

    if isinstance(command, (list, tuple)):
        argv = [str(x) for x in command]
    else:
        argv = str(command or "").split()
    if len(argv) < 2:
        return False
    cmd_str = " ".join(argv)
    if any(ch in cmd_str for ch in _SHELL_OPERATOR_CHARS):
        return False
    if DESTRUCTIVE_COMMAND_RE.search(cmd_str) or NETWORK_COMMAND_RE.search(cmd_str):
        return False
    if SENSITIVE_PATH_RE.search(cmd_str):
        return False
    if argv[0].rsplit("/", 1)[-1] not in _WRITE_RUN_INTERPRETERS:
        return False
    expected = (target_path or "").strip()
    if not expected:
        return False
    script = argv[1]
    return script == expected or script.rsplit("/", 1)[-1] == expected.rsplit("/", 1)[-1]


class TaskNotFound(LookupError): pass
class IdempotencyConflict(ValueError): pass
class LeaseConflict(RuntimeError): pass
class SensitiveTaskInput(ValueError): pass

# InvalidTransition/ConcurrentUpdate come from durable.state_machine and are
# re-exported so existing importers keep the same exception identity while the
# transition graph lives in exactly one place.
__all__ = [
    "ConcurrentUpdate",
    "CreateTask",
    "IdempotencyConflict",
    "InvalidTransition",
    "LeaseConflict",
    "SensitiveTaskInput",
    "TaskNotFound",
    "TaskRepository",
]

@dataclass(frozen=True)
class CreateTask:
    owner_id: str
    goal: str
    path: str
    content: str
    idempotency_key: str
    tool_name: str = "workspace.write_text"
    command: tuple[str, ...] = ()
    mcp_server: str = ""
    mcp_tool: str = ""
    mcp_arguments: dict[str, object] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    read_after_write: bool = False
    # B5: compound "write the script, then run it". The run argv lives on the
    # SECOND step (sandbox.shell) — the task itself stays a workspace write, so
    # the named target path is preserved instead of being lost to a raw
    # `sh -c "<goal>"` plan.
    run_after_write: bool = False
    run_command: tuple[str, ...] = ()
    # L7-1: compound diagnostic fix run (write -> run -> fix-write -> rerun)
    fix_after_run: bool = False
    fix_content: str = ""
    fix_command: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(
            {
                "goal": self.goal,
                "path": self.path,
                "content": self.content,
                "tool_name": self.tool_name,
                "command": self.command,
                "mcp_server": self.mcp_server,
                "mcp_tool": self.mcp_tool,
                "mcp_arguments": self.mcp_arguments,
                "params": self.params,
                "read_after_write": self.read_after_write,
                "fix_after_run": self.fix_after_run,
                "fix_content": self.fix_content,
                "fix_command": self.fix_command,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def legacy_fingerprint(self) -> str:
        raw = json.dumps(
            {
                "goal": self.goal,
                "path": self.path,
                "content": self.content,
                "tool_name": self.tool_name,
                "command": self.command,
                "mcp_server": self.mcp_server,
                "mcp_tool": self.mcp_tool,
                "mcp_arguments": self.mcp_arguments,
                "params": self.params,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def arguments_sha256(self) -> str:
        raw = json.dumps(
            {
                "tool_name": self.tool_name,
                "path": self.path,
                "content_sha256": hashlib.sha256(self.content.encode()).hexdigest(),
                "command": self.command,
                "mcp_server": self.mcp_server,
                "mcp_tool": self.mcp_tool,
                "mcp_arguments": self.mcp_arguments,
                "params": self.params,
                "read_after_write": self.read_after_write,
                "fix_after_run": self.fix_after_run,
                "fix_content": self.fix_content,
                "fix_command": self.fix_command,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()


def _sanitization_changes(value: str) -> bool:
    # Compare against the security projection (redaction + traceback omission)
    # WITHOUT HTML escaping and WITHOUT control-character removal: the
    # task-creation gate detects secret leakage, not rendering or formatting
    # artifacts. Otherwise any task whose text contains a double quote
    # (" -> &quot; under html.escape) or a newline (removed as a Cc control
    # char) would be falsely rejected as sensitive (SensitiveTaskInput) and
    # could never be created.
    projected = sanitize_result_text(
        value,
        max_length=max(1, len(value)),
        escape_html=False,
        remove_controls=False,
    )
    return projected != value


def _validate_create_input(command: CreateTask) -> None:
    sensitive = (
        is_sensitive_path(command.path)
        or is_sensitive_command(command.command)
        or _sanitization_changes(command.content)
        or _sanitization_changes(command.goal)
    )
    if sensitive:
        raise SensitiveTaskInput("task input rejected by safety policy")


def _arguments_digest(arguments: dict[str, object]) -> str:
    candidate = arguments.get("arguments_sha256")
    if (
        isinstance(candidate, str)
        and len(candidate) == 64
        and all(character in "0123456789abcdef" for character in candidate.casefold())
    ):
        return candidate.casefold()
    raw = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class TaskRepository:
    def __init__(self, session: Session, state_cache: StateCache | None = None) -> None:
        self.session = session
        # P4.3: optional cache-aside state cache. It is written only from the
        # after-commit hook (see StateCache.stage) and never consulted here —
        # the graph check and the revision CAS below stay the sole authority.
        self.state_cache = state_cache

    def create(self, command: CreateTask, correlation_id: str | None = None) -> tuple[TaskFlow, bool]:
        # This gate must run before the SELECT below: SQLAlchemy may autoflush
        # pending objects before a query, so even lookup-first would be too late.
        _validate_create_input(command)
        existing = self.session.scalar(select(TaskFlow).where(TaskFlow.owner_id == command.owner_id, TaskFlow.idempotency_key == command.idempotency_key))
        if existing:
            if existing.payload_fingerprint != command.fingerprint and existing.payload_fingerprint != command.legacy_fingerprint:
                raise IdempotencyConflict("idempotency key is bound to another payload") from None
            return self.get(existing.id, command.owner_id), False
        tool_arguments: dict[str, object]
        if command.tool_name == "sandbox.shell":
            # Shell execution needs the classified safe argv. Other request data
            # stays in dedicated fields and is represented here only by a digest.
            tool_arguments = {
                "command": list(command.command),
                "arguments_sha256": command.arguments_sha256,
            }
        elif command.tool_name == "mcp":
            # MCP execution needs the server name, the remote tool name and its
            # arguments; the rest of the request stays in dedicated fields.
            tool_arguments = {
                "server": command.mcp_server,
                "tool": command.mcp_tool,
                "arguments": command.mcp_arguments,
                "arguments_sha256": command.arguments_sha256,
            }
        elif command.params:
            # Generic tool params (e.g. send_email: to/subject/body/attachment).
            tool_arguments = {
                **command.params,
                "arguments_sha256": command.arguments_sha256,
            }
        else:
            tool_arguments = {
                "path": command.path,
                "arguments_sha256": command.arguments_sha256,
            }
        task = TaskFlow(owner_id=command.owner_id, goal=command.goal, target_path=command.path, content=command.content, idempotency_key=command.idempotency_key, payload_fingerprint=command.fingerprint, tool_name=command.tool_name, tool_arguments=tool_arguments)
        self.session.add(task)
        try:
            self.session.flush()
            step = FlowStep(
                task_id=task.id,
                index=0,
                title=f"Execute {command.tool_name} and verify" if not (command.read_after_write or command.run_after_write or command.fix_after_run) else f"Execute {command.tool_name}",
                tool_name=command.tool_name,
                arguments={
                    **tool_arguments,
                    "content": command.content,
                },
                input={
                    "tool_name": command.tool_name,
                    "arguments_sha256": command.arguments_sha256,
                },
            )
            self.session.add(step)
            if command.read_after_write:
                step1 = FlowStep(
                    task_id=task.id,
                    index=1,
                    title="Execute workspace.read_text",
                    tool_name="workspace.read_text",
                    arguments={"path": command.path},
                    input={
                        "tool_name": "workspace.read_text",
                        "path": command.path,
                    },
                )
                self.session.add(step1)
            if command.fix_after_run:
                # L7-1: step 1 (run initial/buggy script)
                run_cmd1 = list(command.run_command or command.command)
                run_step1 = FlowStep(
                    task_id=task.id,
                    index=1,
                    title="Execute sandbox.shell (observe)",
                    tool_name="sandbox.shell",
                    arguments={"command": run_cmd1},
                    input={
                        "tool_name": "sandbox.shell",
                        "command": run_cmd1,
                    },
                )
                self.session.add(run_step1)
                # Step 2: workspace.write_text (write fixed content)
                fix_content = command.fix_content if command.fix_content is not None else command.content
                if not (fix_content and fix_content.strip()):
                    raise SensitiveTaskInput("fix_after_run requires non-empty fix_content")
                fix_sha256 = hashlib.sha256(fix_content.encode()).hexdigest()
                fix_write_step = FlowStep(
                    task_id=task.id,
                    index=2,
                    title="Execute workspace.write_text (fix)",
                    tool_name="workspace.write_text",
                    arguments={
                        "path": command.path,
                        "content": fix_content,
                        "arguments_sha256": fix_sha256,
                    },
                    input={
                        "tool_name": "workspace.write_text",
                        "arguments_sha256": fix_sha256,
                    },
                )
                self.session.add(fix_write_step)
                # Step 3: sandbox.shell (rerun fixed script)
                rerun_cmd = list(command.fix_command or command.run_command or command.command)
                run_step2 = FlowStep(
                    task_id=task.id,
                    index=3,
                    title="Execute sandbox.shell (rerun)",
                    tool_name="sandbox.shell",
                    arguments={"command": rerun_cmd},
                    input={
                        "tool_name": "sandbox.shell",
                        "command": rerun_cmd,
                    },
                )
                self.session.add(run_step2)
            elif command.run_after_write and command.run_command:
                run_step = FlowStep(
                    task_id=task.id,
                    index=1 if not command.read_after_write else 2,
                    title="Execute sandbox.shell",
                    tool_name="sandbox.shell",
                    arguments={"command": list(command.run_command)},
                    input={
                        "tool_name": "sandbox.shell",
                        "command": list(command.run_command),
                    },
                )
                self.session.add(run_step)
            self.session.flush()
            self._journal(task.id, task.id, "task", None, TaskState.RECEIVED.value, "gateway accepted task", "gateway", correlation_id)
            self.session.commit()
            return self.get(task.id, command.owner_id), True
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(select(TaskFlow).where(TaskFlow.owner_id == command.owner_id, TaskFlow.idempotency_key == command.idempotency_key))
            if not existing: raise
            if existing.payload_fingerprint != command.fingerprint and existing.payload_fingerprint != command.legacy_fingerprint:
                raise IdempotencyConflict("idempotency key is bound to another payload") from None
            return self.get(existing.id, command.owner_id), False

    def get(self, task_id: str, owner_id: str | None = None) -> TaskFlow:
        stmt = select(TaskFlow).where(TaskFlow.id == task_id).options(selectinload(TaskFlow.steps), selectinload(TaskFlow.transitions), selectinload(TaskFlow.artifacts), selectinload(TaskFlow.approvals)).execution_options(populate_existing=True)
        if owner_id is not None: stmt = stmt.where(TaskFlow.owner_id == owner_id)
        task = self.session.scalar(stmt)
        if not task: raise TaskNotFound(task_id)
        task.steps.sort(key=lambda x: x.index); task.transitions.sort(key=lambda x: x.id)
        return task

    def _journal(self, task_id: str, entity_id: str, entity_type: str, old: str | None, new: str, reason: str, actor: str, correlation_id: str | None = None) -> None:
        correlation=correlation_id or str(uuid.uuid4())
        transition=StateTransition(task_id=task_id, entity_id=entity_id, entity_type=entity_type, from_state=old, to_state=new, reason=reason, actor=actor, correlation_id=correlation)
        self.session.add(transition); self.session.flush()
        key=f"transition:{transition.id}"
        self.session.add(DeliveryOutbox(task_id=task_id,adapter="progress",event_type="transition",idempotency_key=key,payload={"task_id":task_id,"session_id":"task-owner","correlation_id":correlation,"step_id":entity_id if entity_type=="step" else None,"status":new,"message":reason}))

    def transition(self, task: TaskFlow, target: TaskState, reason: str, actor: str, *, correlation_id: str | None = None) -> None:
        if target is TaskState.DONE: raise InvalidTransition("DONE is not exposed by repository API")
        self._transition(task, target, reason, actor, correlation_id)

    def _transition(self, task: TaskFlow, target: TaskState, reason: str, actor: str, correlation_id: str | None = None) -> None:
        self.session.flush()
        self.session.expire_all()
        current = TaskState(task.status)
        if target is TaskState.DONE: raise InvalidTransition("DONE requires verifier service DB capability")
        try:
            check_task_transition(current, target, cancellation_requested=task.cancellation_requested)
        except InvalidTransition:
            record_rejection(self.session, task_id=task.id, entity_id=task.id, entity_type="task", from_state=current.value, to_state=target.value, reason=reason, actor=actor, correlation_id=correlation_id)
            raise
        expected = task.revision
        result = self.session.execute(update(TaskFlow).where(TaskFlow.id == task.id, TaskFlow.revision == expected).values(status=target.value, revision=expected + 1, updated_at=utcnow()))
        assert isinstance(result, CursorResult)
        if result.rowcount != 1: raise ConcurrentUpdate("revision CAS failed")
        task.status = target.value; task.revision = expected + 1
        self._journal(task.id, task.id, "task", current.value, target.value, reason, actor, correlation_id)
        self.session.flush()
        if self.state_cache is not None:
            self.state_cache.stage(self.session, task.id, target.value)

    def transition_step(self, task: TaskFlow, step: FlowStep, target: StepState, reason: str, actor: str, correlation_id: str | None = None) -> None:
        current=StepState(step.status)
        try:
            check_step_transition(current, target)
        except InvalidTransition:
            record_rejection(self.session, task_id=task.id, entity_id=step.id, entity_type="step", from_state=current.value, to_state=target.value, reason=reason, actor=actor, correlation_id=correlation_id)
            raise
        expected = step.revision if step.revision is not None else 0
        result=self.session.execute(update(FlowStep).where(FlowStep.id==step.id,FlowStep.revision==expected).values(status=target.value,revision=expected+1))
        assert isinstance(result,CursorResult)
        if result.rowcount != 1: raise ConcurrentUpdate("step revision CAS failed")
        old=step.status; step.status=target.value; step.revision=expected+1
        self._journal(task.id, step.id, "step", old, target.value, reason, actor, correlation_id); self.session.flush()

    def acquire_lease(self, task: TaskFlow, worker: str, seconds: int) -> None:
        self.session.flush()
        self.session.expire_all()
        now = utcnow(); expected = task.revision
        result = self.session.execute(update(TaskFlow).where(TaskFlow.id == task.id, TaskFlow.revision == expected, (TaskFlow.lease_expires_at.is_(None)) | (TaskFlow.lease_expires_at < now) | (TaskFlow.lease_owner == worker)).values(lease_owner=worker, lease_expires_at=now + timedelta(seconds=seconds), heartbeat_at=now))
        assert isinstance(result, CursorResult)
        if result.rowcount != 1: raise LeaseConflict("task already has an active writer")
        task.lease_owner=worker; task.lease_expires_at=now+timedelta(seconds=seconds); task.heartbeat_at=now; self.session.commit()

    def heartbeat(self, task: TaskFlow, worker: str, seconds: int) -> None:
        now=utcnow(); result=self.session.execute(update(TaskFlow).where(TaskFlow.id==task.id,TaskFlow.lease_owner==worker).values(lease_expires_at=now+timedelta(seconds=seconds),heartbeat_at=now))
        assert isinstance(result,CursorResult)
        if result.rowcount!=1: raise LeaseConflict("lease not owned")
        self.session.commit()

    def release_lease(self, task: TaskFlow, worker: str) -> None:
        self.session.execute(update(TaskFlow).where(TaskFlow.id == task.id, TaskFlow.lease_owner == worker).values(lease_owner=None, lease_expires_at=None)); self.session.commit(); task.lease_owner=None

    def request_approval(self, task: TaskFlow) -> Approval:
        existing = next((a for a in task.approvals if a.tool_name == task.tool_name), None)
        if existing: return existing
        from antigona.worker.hitl import RiskLevel, evaluate_risk, get_confirmation_policy
        risk_level, _ = evaluate_risk(task.tool_name, task.tool_arguments, task.target_path)
        # B5: a compound task can carry a sandbox.shell step even when the task
        # tool is a workspace write. The shell gate must not be bypassed by the
        # lower-risk task tool — take the HIGHEST risk across the planned steps.
        # B5 (live round 6) exception: for the deterministic write→run compound
        # the run step is built by the system from the parsed goal and merely
        # executes the script the write step just created. That step must NOT
        # drag the task to MEDIUM and hang the flow in WAITING_APPROVAL — the
        # command still has to be provably safe (see the helper). Every other
        # sandbox.shell step keeps the fail-closed max-risk behaviour.
        _write_run = False
        try:
            from antigona.task_goal import parse_goal

            _write_run = parse_goal(task.goal or "").intent in ("file_write_run", "file_write_fix_run")
        except Exception:  # pragma: no cover - parser must never break approval
            _write_run = False
        _rank = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
        for _step in task.steps:
            if _step.tool_name != "sandbox.shell":
                continue
            if _write_run and _is_deterministic_write_run_command(
                (_step.arguments or {}).get("command"), task.target_path
            ):
                continue
            _step_risk, _ = evaluate_risk(
                "sandbox.shell", dict(_step.arguments or {}), task.target_path
            )
            if _rank.get(_step_risk, 1) > _rank.get(risk_level, 1):
                risk_level = _step_risk
        policy = get_confirmation_policy()
        decision = "APPROVED" if policy.should_auto_approve(risk_level) else "PENDING"
        approval=Approval(
            task_id=task.id,
            tool_name=task.tool_name,
            arguments={
                "tool_name": task.tool_name,
                "arguments_sha256": _arguments_digest(task.tool_arguments),
            },
            risk_level=risk_level.value,
            reason=f"{risk_level.value.lower()} risk tool requires confirmation",
            decision=decision,
        )
        self.session.add(approval); task.approvals.append(approval); self.session.flush(); return approval

    def decide_approval(self, task: TaskFlow, approval_id: str, owner: str, approve: bool) -> Approval:
        approval=next((a for a in task.approvals if a.id == approval_id), None)
        if not approval or approval.decision != "PENDING": raise ValueError("approval unavailable")
        if approve:
            # A-1: an owner approval is a one-shot grant, not a string. It is
            # minted here (APPROVAL time) bound to actor + tool + the exact
            # recorded arguments digest, and consumed exactly once by the
            # executing path. Minting failure => nothing is decided, the
            # approval stays PENDING (fail-closed), never a bare "APPROVED".
            approval.grant_token = self._mint_approval_grant(task, approval, owner)
        approval.decision="APPROVED" if approve else "DENIED"; approval.decided_by=owner; approval.decided_at=utcnow(); self.session.commit(); return approval

    @staticmethod
    def _mint_approval_grant(task: TaskFlow, approval: Approval, owner: str) -> str:
        """Issue the canonical durable one-shot grant for an owner approval. Returns token_hash."""
        from .security.approval_grant import ApprovalGrantStore, _hash_token

        try:
            raw_token = ApprovalGrantStore().issue(
                actor=str(owner),
                tool_name=str(approval.tool_name),
                args=dict(approval.arguments or {}),
                issuer="owner-approval",
                ttl_seconds=APPROVAL_GRANT_TTL_SECONDS,
                one_shot=True,
                session_id=str(task.id),
                reason=str(approval.reason or ""),
            )
            return _hash_token(raw_token)
        except Exception as exc:  # noqa: BLE001 — fail-closed boundary
            raise ValueError(f"approval grant could not be issued: {exc}") from exc

    def cancel(self, task: TaskFlow, correlation_id: str | None = None) -> TaskFlow:
        if TaskState(task.status) in TERMINAL: return task
        task.cancellation_requested=True
        for step in task.steps:
            if step.status in {StepState.PENDING.value, StepState.RUNNING.value}: self.transition_step(task, step, StepState.CANCELLED, "task cancelled", "gateway", correlation_id)
        self.transition(task, TaskState.CANCELLED, "user requested cancellation", "gateway", correlation_id=correlation_id); self.session.commit(); return self.get(task.id)

    def steer(self, task: TaskFlow, message: str, actor: str = "gateway", correlation_id: str | None = None) -> TaskFlow:
        current = str(task.status).upper()
        if current not in {"WAITING_APPROVAL", "RUNNING"}:
            raise InvalidTransition(f"Steering not allowed for flow in status {task.status}")
        tool_args = dict(task.tool_arguments or {})
        steer_list = list(tool_args.get("steer_messages") or [])
        steer_list.append(message)
        tool_args["steer_messages"] = steer_list
        task.tool_arguments = tool_args
        task.updated_at = utcnow()
        self._journal(task.id, task.id, "task", task.status, task.status, f"steer: {message}", actor, correlation_id)
        self.session.commit()
        return self.get(task.id)

    def commit(self) -> None: self.session.commit()

