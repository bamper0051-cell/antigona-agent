from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import ArtifactResult, ToolResult, WriteFileInput
from .durable.state_cache import StateCache
from .filesystem import WorkspaceFileTool
from .models import (
    Approval,
    Artifact,
    DurableOperation,
    FlowStep,
    StepState,
    TaskFlow,
    TaskState,
    utcnow,
)
from .ownership.epoch import FenceDeniedError, OwnershipContext
from .ownership.wiring import enforce_write_fence
from .pipeline import CompletionVerifier, DeterministicPlanner, Planner
from .repository import LeaseConflict, TaskRepository
from .result_safety import (
    is_sensitive_execution,
    is_usable_result_text,
    project_tool_result,
    sanitize_result_text,
)
from .shell import DockerShellTool, ShellInput
from .task_goal import ANSWER_ONLY_TOOL, WRITE_TOOL_NAMES, canonical_tool_name
from .tools.workspace_read import WorkspaceReadTextTool
from .workspace import BaseWorkspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpCallOutcome:
    """Typed outcome of one MCP tool invocation.

    ``reason`` is a stable machine code so callers can name the REAL failure
    instead of collapsing every case into one generic message:

    * ``ok``                  - the tool ran and returned a result;
    * ``unregistered_server`` - the server is not in the MCP registry;
    * ``timeout``             - the bridge did not return within the budget;
    * ``bridge_error``        - connect/call raised, or an unknown failure.
    """

    ok: bool
    value: str | None = None
    reason: str = "ok"
    detail: str = ""


def _registered_mcp_server_names() -> list[str]:
    """Names of MCP servers currently registered (best-effort, never raises)."""
    try:
        from antigona.core.mcp import MCPRegistry

        return sorted(MCPRegistry.load().names())
    except Exception:  # pragma: no cover - registry load must never break a call
        return []


def _mcp_failure_message(server: str, tool: str, outcome: McpCallOutcome) -> str:
    """Concrete, actionable failure text for a failed MCP call.

    Never returns a bare generic string: the reason is named, and for an
    unregistered server the known servers are listed so the caller can act.
    """
    if outcome.reason == "unregistered_server":
        known = _registered_mcp_server_names()
        tail = ", ".join(known) if known else "none"
        return (
            f"mcp server '{server}' is not registered "
            f"(registered: {tail}) - capability unavailable"
        )
    if outcome.reason == "timeout":
        return f"mcp call '{server}.{tool}' timed out: {outcome.detail}"
    if outcome.reason == "bridge_error":
        return f"mcp call '{server}.{tool}' failed: {outcome.detail}"
    return f"mcp call '{server}.{tool}' failed: {outcome.reason or 'unknown error'}"


def _run_async_mcp_call(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> McpCallOutcome:
    """Invoke one MCP tool on a registered server, bridging async -> sync.

    The worker loop is synchronous, so the async MCP client (``core.mcp``)
    is run on a dedicated event loop. When called from within a running loop
    (async tests / callers) the call is bridged through a worker thread so
    the sync Orchestrator contract is preserved either way.

    Returns a typed :class:`McpCallOutcome` - never a bare ``None`` - so the
    caller can distinguish unregistered / timeout / bridge error.
    """

    async def _call() -> McpCallOutcome:
        from antigona.core.mcp import MCPRegistry, connect_from_entry

        reg = MCPRegistry.load()
        entry = reg.servers.get(server)
        if entry is None:
            logger.warning("mcp server '%s' is not registered", server)
            return McpCallOutcome(
                ok=False,
                reason="unregistered_server",
                detail=f"server '{server}' is not registered",
            )
        try:
            client = await connect_from_entry(entry)
        except Exception as exc:
            logger.warning("mcp server '%s' connect failed: %s", server, exc)
            return McpCallOutcome(
                ok=False,
                reason="bridge_error",
                detail=f"could not connect to server '{server}' ({type(exc).__name__})",
            )
        try:
            raw = await client.call_tool(tool, arguments)
            return McpCallOutcome(
                ok=True, value=raw if isinstance(raw, str) else str(raw)
            )
        except Exception as exc:
            logger.warning("mcp tool %s.%s failed: %s", server, tool, exc)
            return McpCallOutcome(
                ok=False,
                reason="bridge_error",
                detail=f"tool '{tool}' on server '{server}' raised {type(exc).__name__}",
            )
        finally:
            await client.aclose()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_call())

    outcome: dict[str, McpCallOutcome] = {}

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(_call())
        except Exception as exc:  # pragma: no cover - defensive bridge guard
            logger.warning("mcp bridge runner failed: %s", exc)
            outcome["value"] = McpCallOutcome(
                ok=False,
                reason="bridge_error",
                detail=f"bridge failed ({type(exc).__name__})",
            )

    bridge = threading.Thread(target=_runner, daemon=True)
    bridge.start()
    bridge.join(timeout=timeout_seconds)
    if "value" not in outcome:
        logger.warning(
            "mcp call %s.%s timed out after %.1fs", server, tool, timeout_seconds
        )
        return McpCallOutcome(
            ok=False,
            reason="timeout",
            detail=f"call '{tool}' did not return within {timeout_seconds:.0f}s",
        )
    return outcome["value"]


class SubagentError(Exception):
    """Base exception for subagent errors."""


class DepthLimitExceeded(SubagentError):
    """Raised when spawning a child flow exceeds the maximum depth limit."""


class BudgetLimitExceeded(SubagentError):
    """Raised when spawning a child flow exceeds the maximum child task budget."""


@dataclass(frozen=True)
class QueueItem:
    task_id: str
    correlation_id: str


class TaskQueue:
    """Durable-state queue abstraction; SQLite task status is the P0 backing store."""

    def enqueue(self, task: TaskFlow) -> QueueItem:
        return QueueItem(task.id, str(uuid.uuid4()))


class Orchestrator:
    def __init__(
        self,
        session: Session,
        tool: WorkspaceFileTool,
        verifier: CompletionVerifier,
        *,
        shell_tool: DockerShellTool | None = None,
        workspace: BaseWorkspace | None = None,
        planner: Planner | None = None,
        lease_seconds: int = 30,
        max_retries: int = 1,
        state_cache: StateCache | None = None,
    ) -> None:
        # P4.3: the cache is write-after-commit only; passing None (the default)
        # leaves the orchestrator byte-for-byte equivalent to its pre-P4.3 behaviour.
        self.repository = TaskRepository(session, state_cache)
        self.tool = tool
        self.shell_tool = shell_tool
        self.workspace = workspace
        self.verifier = verifier
        #: Live ownership fencing token (DF-WO2-003-residual); forwarded from the
        #: bound workspace so the mcp file-artifact write is fenced too.  None
        #: when ownership is disabled (backward-compat).
        self.ownership: OwnershipContext | None = (
            getattr(workspace, "ownership", None)
            if workspace is not None
            else getattr(tool, "ownership", None)
        )
        if self.ownership is not None:
            if hasattr(self.tool, "bind_ownership"):
                self.tool.bind_ownership(self.ownership)
            if self.shell_tool is not None and hasattr(self.shell_tool, "bind_ownership"):
                self.shell_tool.bind_ownership(self.ownership)
        self.planner = planner or DeterministicPlanner()
        self.lease_seconds = lease_seconds
        self.max_retries = max_retries

    def run(self, task: TaskFlow, worker_id: str | None = None) -> TaskFlow:
        worker = worker_id or str(uuid.uuid4())
        correlation = str(uuid.uuid4())
        state = TaskState(task.status)
        if task.cancellation_requested or state in {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
            TaskState.TIMEOUT,
            TaskState.POLICY_DENIED,
        }:
            return task
        self.repository.acquire_lease(task, worker, self.lease_seconds)
        try:
            return self._resume(task, correlation)
        finally:
            try:
                self.repository.release_lease(task, worker)
            except LeaseConflict:
                pass

    def _resume(self, task: TaskFlow, correlation: str) -> TaskFlow:
        step = task.steps[0]
        state = TaskState(task.status)
        if state is TaskState.RECEIVED:
            self.repository.transition(
                task, TaskState.QUEUED, "queued", "queue", correlation_id=correlation
            )
            self.repository.commit()
            state = TaskState.QUEUED
        if state is TaskState.QUEUED:
            plan = self.planner.plan(task)
            self.repository.transition(
                task,
                TaskState.PLANNING,
                f"plan with {len(plan.steps)} steps persisted",
                "orchestrator",
                correlation_id=correlation,
            )
            task.checkpoint = "planned"
            self.repository.commit()
            state = TaskState.PLANNING
        if state is TaskState.PLANNING:
            approval = self.repository.request_approval(task)
            if approval.decision == "PENDING":
                self.repository.transition(
                    task,
                    TaskState.WAITING_APPROVAL,
                    f"{approval.risk_level}-risk tool requires approval: {approval.reason}",
                    "policy",
                    correlation_id=correlation,
                )
                task.checkpoint = "approval_requested"
                self.repository.commit()
                return self.repository.get(task.id)
            if approval.decision == "DENIED":
                self.repository.transition(
                    task,
                    TaskState.POLICY_DENIED,
                    "approval denied",
                    "policy",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return self.repository.get(task.id)
            if approval.decision == "APPROVED":
                if not self._verify_and_consume_approval(task, approval, step, correlation):
                    return self.repository.get(task.id)
            self._dispatch(task, step, correlation)
            state = TaskState.TOOL_EXECUTING
        if state is TaskState.WAITING_APPROVAL:
            # Resume on the concrete PENDING approval, not approvals[0] (which
            # may already be decided). Fall back to the first entry so the
            # DENIED/APPROVED branches keep a stable reference.
            resume_approval: Approval | None = next(
                (a for a in task.approvals if a.decision == "PENDING"), None
            )
            if resume_approval is None and task.approvals:
                resume_approval = task.approvals[0]
            if resume_approval is not None and resume_approval.decision == "PENDING":
                from antigona.worker.hitl import check_approval_timeout, get_confirmation_policy
                policy = get_confirmation_policy()
                if check_approval_timeout(resume_approval, policy.timeout_seconds):
                    self.repository.decide_approval(task, resume_approval.id, owner="system_timeout", approve=False)
                    self.repository.transition(
                        task,
                        TaskState.POLICY_DENIED,
                        f"approval timed out after {policy.timeout_seconds}s (auto-rejected)",
                        "policy",
                        correlation_id=correlation,
                    )
                    self.repository.commit()
                    return self.repository.get(task.id)
                return task
            if resume_approval is not None and resume_approval.decision == "DENIED":
                self.repository.transition(
                    task,
                    TaskState.POLICY_DENIED,
                    "approval denied",
                    "policy",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return self.repository.get(task.id)
            if resume_approval is not None and resume_approval.decision == "APPROVED":
                if not self._verify_and_consume_approval(task, resume_approval, step, correlation):
                    return self.repository.get(task.id)
            self._dispatch(task, step, correlation)
            state = TaskState.TOOL_EXECUTING
        if state is TaskState.TOOL_EXECUTING:
            if self._block_sensitive_execution(task, correlation):
                return self.repository.get(task.id)
            artifact = self._recover_or_execute(task, step, correlation)
            if artifact is None:
                return self.repository.get(task.id)
            self.repository.transition(
                task,
                TaskState.OBSERVING,
                "tool evidence persisted",
                "orchestrator",
                correlation_id=correlation,
            )
            task.checkpoint = "observed"
            self.repository.commit()
            state = TaskState.OBSERVING
        if state is TaskState.OBSERVING:
            if not task.artifacts:
                recovered = self._recover_or_execute(task, step, correlation)
                if recovered is None:
                    return self.repository.get(task.id)
                self.repository.commit()
            self.repository.transition(
                task,
                TaskState.VERIFYING,
                "completion requested",
                "orchestrator",
                correlation_id=correlation,
            )
            task.checkpoint = "verifying"
            self.repository.commit()
            state = TaskState.VERIFYING
        if state is TaskState.VERIFYING:
            if not task.artifacts:
                recovered = self._recover_or_execute(task, step, correlation)
                if recovered is None:
                    return self.repository.get(task.id)
                self.repository.commit()
            verifier_unavailable = False
            try:
                decision = self.verifier.request_verification(task.id, correlation)
            except Exception:
                # Provider/client exception detail is untrusted and must never reach
                # worker retries, transitions, outbox payloads, or public state.
                decision = "REPLAN"
                verifier_unavailable = True
            if verifier_unavailable:
                task = self.repository.get(task.id)
                if TaskState(task.status) is TaskState.VERIFYING:
                    self.repository.transition(
                        task,
                        TaskState.FAILED,
                        "verification service unavailable",
                        "verifier-client",
                        correlation_id=correlation,
                    )
                    self.repository.commit()
                return self.repository.get(task.id)
            if decision != "DONE":
                # The verifier service owns the terminal VERIFYING -> {DONE, FAILED}
                # edge and commits it on its own connection *before* answering. Our
                # in-memory task therefore predates that write, and re-issuing the
                # same terminal transition from here is a duplicate: it loses the
                # revision CAS (or is refused as FAILED -> FAILED) and kills a job
                # whose outcome is already durably recorded. Re-read the
                # authoritative row and only record the failure when the verifier
                # declined without writing one.
                task = self.repository.get(task.id)
                if TaskState(task.status) is TaskState.VERIFYING:
                    self.repository.transition(
                        task,
                        TaskState.FAILED,
                        "verification requested replan",
                        "verifier-client",
                        correlation_id=correlation,
                    )
                    self.repository.commit()
        return self.repository.get(task.id)

    def _verify_and_consume_approval(
        self, task: TaskFlow, approval: Approval, step: object, correlation: str
    ) -> bool:
        """Verify and consume the owner's one-shot approval grant on dispatch.

        Checks:
        1. Approval decision is time-bounded (TTL 3600s / APPROVAL_GRANT_TTL_SECONDS).
        2. If a grant_token is present, atomically verify and consume it via ApprovalGrantStore.
        3. If grant_token is absent, verify that the risk level is eligible for auto-approval.
           If not, deny execution (fail-closed).
        """
        from antigona.repository import APPROVAL_GRANT_TTL_SECONDS
        from antigona.security.approval_grant import ApprovalGrantStore
        from antigona.worker.hitl import RiskLevel, get_confirmation_policy

        # Check time-bounding / TTL on the approval decision
        if approval.decided_at is not None:
            now = utcnow()
            decided_at = approval.decided_at
            if decided_at.tzinfo is None and now.tzinfo is not None:
                decided_at = decided_at.replace(tzinfo=datetime.UTC)
            elif decided_at.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=datetime.UTC)
            age = (now - decided_at).total_seconds()
            if age > APPROVAL_GRANT_TTL_SECONDS:
                self.repository.transition(
                    task,
                    TaskState.POLICY_DENIED,
                    f"approval grant expired (age={age:.0f}s > {APPROVAL_GRANT_TTL_SECONDS}s)",
                    "policy",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return False

        grant_token = approval.grant_token
        if not grant_token:
            # If no grant token, only auto-approved low-risk tools are permitted
            policy = get_confirmation_policy()
            try:
                risk = RiskLevel(str(approval.risk_level).upper())
            except Exception:
                risk = RiskLevel.HIGH
            if not policy.should_auto_approve(risk):
                self.repository.transition(
                    task,
                    TaskState.POLICY_DENIED,
                    "approval grant missing for non-auto-approved tool",
                    "policy",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return False
            return True

        # Atomically verify and consume the grant
        try:
            verdict = ApprovalGrantStore().verify_and_consume_stored(
                grant_token,
                actor=str(approval.decided_by or "owner"),
                tool_name=str(approval.tool_name),
                args=dict(approval.arguments or {}),
                consumed_by=f"orchestrator:{task.id}",
            )
        except Exception:
            logger.exception("Approval grant consumption failed in orchestrator")
            verdict = None

        if verdict is None or not verdict.valid:
            reason = f"approval grant invalid ({getattr(verdict, 'reason', 'store_error')})"
            self.repository.transition(
                task,
                TaskState.POLICY_DENIED,
                reason,
                "policy",
                correlation_id=correlation,
            )
            self.repository.commit()
            return False

        return True

    def _dispatch(self, task: TaskFlow, step: object, correlation: str) -> None:
        typed = step if isinstance(step, FlowStep) else (task.steps[0] if task.steps else None)
        if typed and typed.status == StepState.PENDING.value:
            self.repository.transition_step(
                task, typed, StepState.RUNNING, "tool dispatched", "orchestrator", correlation
            )
        step_id = typed.id if typed else "step0"
        task.side_effect_key = hashlib.sha256(
            f"{task.id}:{step_id}:{task.tool_name}".encode()
        ).hexdigest()
        task.checkpoint = "tool_dispatched"
        self.repository.transition(
            task,
            TaskState.TOOL_EXECUTING,
            "approved sandbox tool dispatched",
            "orchestrator",
            correlation_id=correlation,
        )
        self.repository.commit()

    @staticmethod
    def _step_command(step: object) -> tuple[str, ...]:
        """Argv persisted on a single step (compound write→run flows)."""
        arguments = getattr(step, "arguments", None)
        raw = arguments.get("command", []) if isinstance(arguments, dict) else []
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, (list, tuple)):
            return tuple(str(part) for part in raw)
        return ()

    @staticmethod
    def _step_content(step: object) -> str | None:
        """Content persisted on a single step (compound write→run→fix→run flows)."""
        arguments = getattr(step, "arguments", None)
        if isinstance(arguments, dict) and "content" in arguments and arguments["content"] is not None:
            return str(arguments["content"])
        input_data = getattr(step, "input", None)
        if isinstance(input_data, dict) and "content" in input_data and input_data["content"] is not None:
            return str(input_data["content"])
        return None

    @staticmethod
    def _command(task: TaskFlow) -> tuple[str, ...]:
        raw = task.tool_arguments.get("command", []) if task.tool_arguments else []
        if isinstance(raw, str):
            return (raw,)
        return tuple(str(part) for part in raw)

    def _execution_workspace_root(self, task: TaskFlow) -> Path | None:
        if task.tool_name == "sandbox.shell" and self.shell_tool is not None:
            shell_root = getattr(self.shell_tool, "workspace", None)
            if shell_root is not None:
                return Path(shell_root)
        backend = getattr(self.tool, "backend", None)
        backend_root = getattr(backend, "workspace", None)
        if backend_root is not None:
            return Path(backend_root)
        if self.workspace is not None:
            return self.workspace.root_path
        return None

    def _block_sensitive_execution(self, task: TaskFlow, correlation: str) -> bool:
        command = self._command(task)
        workspace_root = self._execution_workspace_root(task)
        # B5: compound write→run tasks carry the argv on a shell STEP; it must
        # pass the same safety classification as a task-level command.
        candidates = [command, *(self._step_command(s) for s in task.steps)]
        if not any(
            is_sensitive_execution(
                task.target_path,
                candidate,
                content=task.content,
                workspace_root=workspace_root,
            )
            for candidate in candidates
        ):
            return False

        step = task.steps[0]
        projection = project_tool_result(
            ToolResult(False, "failed", error="blocked by safety policy"),
            path=task.target_path,
            command=command,
            content=task.content,
            workspace_root=workspace_root,
        )
        step.output = {
            "ok": False,
            "side_effect_key": f"{task.id}:{step.id}",
            "tool_result": projection,
        }
        if step.status == StepState.PENDING.value:
            self.repository.transition_step(
                task,
                step,
                StepState.RUNNING,
                "Safety classification started",
                "orchestrator",
                correlation,
            )
        if step.status == StepState.RUNNING.value:
            self.repository.transition_step(
                task,
                step,
                StepState.FAILED,
                "Execution blocked by result safety policy",
                "orchestrator",
                correlation,
            )
        self.repository.transition(
            task,
            TaskState.BLOCKED,
            "Execution blocked by result safety policy",
            "orchestrator",
            correlation_id=correlation,
        )
        task.checkpoint = "result-safety-blocked"
        self.repository.commit()
        return True

    def _is_stdout_only_shell_request(self, task: TaskFlow) -> bool:
        target = task.target_path.strip().casefold()
        if target in {"stdout", "stdout.txt"}:
            return True
        command = self._command(task)
        executable = command[0].rsplit("/", 1)[-1].casefold() if command else ""
        if executable.startswith("python") and target in {"py", "python", "python3"}:
            return True
        return executable in {"bash", "sh"} and target in {"bash", "shell", "sh"}

    def _materialize_stdout_artifact(
        self,
        task: TaskFlow,
        result: ToolResult,
        projection: dict[str, Any],
        *,
        include_exit_code: bool = False,
    ) -> ToolResult:
        preview = projection.get("stdout_preview")
        if not isinstance(preview, str) or not is_usable_result_text(preview):
            return ToolResult(False, "failed", error="tool produced no safe result text")
        content = preview
        # FP-L23: for a stdout-only tool (``sandbox.shell``, an MCP text result)
        # the line structure of the output IS the effect trace — the projection
        # deliberately collapses newlines for chat rendering, so re-project the
        # raw stdout here with its newlines kept (exactly as the B5 run-stdout
        # envelope already relies on its line breaks). A two-entry
        # ``ls | head -2`` whose names glued into one 72-character token is no
        # longer recognisable as the command's output: the verifier judge
        # rejected that real, hash-valid effect as "Artifact content not
        # provided" (live witness fa6e102b).
        raw_output = result.data.get("output") if isinstance(result.data, dict) else None
        if isinstance(raw_output, str) and "content" not in (result.data or {}):
            faithful = sanitize_result_text(raw_output, preserve_newlines=True)
            if isinstance(faithful, str) and is_usable_result_text(faithful):
                content = faithful
        if include_exit_code:
            # B5: a write→run compound goal asks for the program stdout AND its
            # exit code; a stdout-only artifact loses half of the answer. The
            # shell tool only reports ``ok`` for a zero return code, so an
            # explicit ``exit_code`` in the result data wins and 0 is the
            # truthful fallback for a completed run.
            raw_code = result.data.get("exit_code", result.data.get("exit"))
            exit_code = str(raw_code) if raw_code is not None else "0"
            content = f"stdout:\n{content.rstrip()}\n\nexit code:\n{exit_code}\n"
        result_path = f".antigona-results/{task.id}.txt"
        try:
            stored = self.tool.execute(WriteFileInput(path=result_path, content=content))
        except Exception:
            return ToolResult(False, "failed", error="safe result artifact unavailable")
        if not stored.ok or not stored.artifacts:
            return ToolResult(False, "failed", error="safe result artifact unavailable")
        # FP-L23R: the artifact above holds the faithful effect trace, but the
        # chat-oriented ``stdout_preview`` projection (result_safety) collapses
        # newlines by design. Carry the faithful text next to the raw data so
        # the step can persist it as ``effect_stdout`` — that is the record the
        # verifier judges, and it must not disagree with the artifact (a glued
        # duplicate next to the real trace is exactly the ambiguity that made
        # the live verdict flip).
        data: dict[str, Any] = dict(result.data) if isinstance(result.data, dict) else {}
        data["stdout_faithful"] = content
        return ToolResult(
            True,
            "completed",
            data=data,
            artifacts=stored.artifacts,
        )

    def _execute_mcp_tool(self, task: TaskFlow) -> ToolResult:
        """Invoke one tool on a registered MCP server.

        Reads server/tool/arguments from the task's persisted tool_arguments
        (written by :class:`~antigona.repository.CreateTask` for mcp tasks),
        connects through the same ``core.mcp`` client the ``mcp`` builtin tool
        uses, and shapes the result like a stdout-only shell turn. When the
        tool returns ``{"file": <path>, ...}`` (e.g. TTS mp3), the path is
        carried in ``data["file"]`` for artifact promotion.
        """
        try:
            arguments = dict((task.tool_arguments or {}).get("arguments") or {})
            server = str((task.tool_arguments or {}).get("server") or "")
            tool = str((task.tool_arguments or {}).get("tool") or "")
            if not server or not tool:
                return ToolResult(False, "failed", error="mcp server/tool not specified")
            outcome = _run_async_mcp_call(server, tool, arguments)
            if not outcome.ok:
                return ToolResult(
                    False, "failed", error=_mcp_failure_message(server, tool, outcome)
                )
            raw = outcome.value or ""
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("file"):
                    return ToolResult(
                        True,
                        "completed",
                        data={"output": raw, "file": str(payload["file"])},
                    )
            except Exception:
                pass  # plain-text result — stdout artifact
            return ToolResult(True, "completed", data={"output": raw})
        except Exception as exc:
            logger.warning("mcp tool execution failed for %s: %s", task.id, exc)
            return ToolResult(
                False,
                "failed",
                error=f"mcp tool execution failed ({type(exc).__name__})",
            )

    def _materialize_mcp_file_artifact(
        self,
        task: TaskFlow,
        result: ToolResult,
        workspace_root: Path | None,
    ) -> ToolResult:
        """Copy a file produced by an MCP tool into the flow's artifact store.

        The tool writes into ``<workspace>/mcp_output/``; we promote it to
        ``.antigona-results/<task_id>.mp3`` (same location stdout artifacts
        use) and hash it directly — ``read_and_hash`` decodes UTF-8 and cannot
        handle binary media.
        """
        try:
            src = Path(str(result.data.get("file"))).resolve()
            root = (workspace_root or self._execution_workspace_root(task) or Path(".")).resolve()
            if not src.is_file() or not src.is_relative_to(root):
                return ToolResult(False, "failed", error="mcp file artifact outside workspace")
            relative = f".antigona-results/{task.id}.mp3"
            destination = root / ".antigona-results" / f"{task.id}.mp3"
            # DF-WO2-003-residual: fence the mutation boundary BEFORE any
            # filesystem side effect.  Ownership DISABLED (default) -> no-op.
            # Ownership ENABLED + stale/unauthorised owner or no token -> denied
            # before ``mkdir``/``copy2`` so no file lands (fail-closed, INV-06).
            try:
                enforce_write_fence(getattr(self, "ownership", None), "mcp_artifact")
            except FenceDeniedError as exc:
                return ToolResult(
                    False,
                    "failed",
                    error=f"protected write denied: {exc.check.reason}",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, destination)
            data = destination.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            return ToolResult(
                True,
                "completed",
                data=result.data,
                artifacts=[ArtifactResult(relative, digest, len(data))],
            )
        except Exception as exc:
            logger.warning("mcp file artifact failed for %s: %s", task.id, exc)
            return ToolResult(False, "failed", error="mcp file artifact unavailable")

    def _execute_send_email(self, task: TaskFlow) -> ToolResult:
        """Deliver an email via the shared Gmail sender (task branch).

        Parameters come from the task's persisted tool_arguments (written by
        ``CreateTask.params`` for send_email tasks): to/subject/body/attachment.
        The attachment path is resolved against the execution workspace.
        """
        try:
            args = dict(task.tool_arguments or {})
            from antigona.core.email_sender import send_email

            attachment = str(args.get("attachment") or "").strip()
            if attachment:
                workspace_root = self._execution_workspace_root(task)
                candidate = Path(attachment)
                if not candidate.is_absolute() and workspace_root is not None:
                    candidate = workspace_root / candidate
                attachment = str(candidate)
            confirmation = send_email(
                to=str(args.get("to") or ""),
                subject=str(args.get("subject") or "Antigona delivery"),
                body=str(args.get("body") or task.goal),
                attachments=[attachment] if attachment else [],
            )
            return ToolResult(True, "completed", data={"output": confirmation})
        except Exception as exc:
            logger.warning("send_email execution failed for %s: %s", task.id, exc)
            return ToolResult(False, "failed", error="send_email execution failed")

    def _recover_or_execute(
        self, task: TaskFlow, step: object, correlation: str
    ) -> Artifact | None:
        steps_to_run = sorted(task.steps, key=lambda s: s.index) if task.steps else []
        if not steps_to_run:
            return None

        # Check if all steps already have artifacts and are COMPLETED
        if all(s.status == StepState.COMPLETED.value for s in steps_to_run) and task.artifacts:
            return task.artifacts[-1]

        last_artifact: Artifact | None = None
        for typed in steps_to_run:
            if typed.status == StepState.COMPLETED.value:
                matching_art = next((a for a in task.artifacts if a.step_id == typed.id), None)
                if matching_art:
                    last_artifact = matching_art
                    continue
                logger.warning("Step %s is COMPLETED but missing artifact in task %s", typed.id, task.id)
                return None

            if typed.status == StepState.FAILED.value:
                return None

            if typed.status == StepState.PENDING.value:
                self.repository.transition_step(
                    task, typed, StepState.RUNNING, "recovered dispatched step", "recovery", correlation
                )
                self.repository.commit()

            step_tool_name = (
                typed.tool_name
                or (typed.input.get("tool_name") if typed.input else None)
                or task.tool_name
            )

            operation_id = hashlib.sha256(
                f"{task.id}:{typed.id}:{step_tool_name}".encode()
            ).hexdigest()

            operation = self.repository.session.scalar(
                select(DurableOperation).where(DurableOperation.id == operation_id)
            )
            if operation and operation.status == "APPLIED" and operation.result:
                stored_result = operation.result
                summary = stored_result.get("tool_result")
                art = self._persist_artifact(
                    task,
                    typed,
                    str(stored_result.get("path") or task.target_path),
                    str(stored_result["sha256"]),
                    int(stored_result["size"]),
                    correlation,
                    "recovered durable operation",
                    summary if isinstance(summary, dict) else None,
                    effect_stdout=(
                        stored_result.get("effect_stdout")
                        if isinstance(stored_result.get("effect_stdout"), str)
                        else None
                    ),
                )
                last_artifact = art
                continue

            if step_tool_name == "workspace.write_text":
                step_content = self._step_content(typed)
                if step_content is None:
                    step_content = task.content
                try:
                    observed, digest = self.tool.read_and_hash(task.target_path)
                    if observed == step_content:
                        art = self._persist_artifact(
                            task,
                            typed,
                            task.target_path,
                            digest,
                            len(observed.encode()),
                            correlation,
                            "recovered completed side effect",
                            {"tool_name": "workspace.write_text", "path": task.target_path},
                        )
                        last_artifact = art
                        continue
                except (OSError, ValueError):
                    pass

            if not operation:
                operation = DurableOperation(
                    id=operation_id,
                    task_id=task.id,
                    step_id=typed.id,
                    kind=step_tool_name,
                    request={
                        "arguments_sha256": hashlib.sha256(
                            json.dumps(task.tool_arguments, sort_keys=True).encode()
                        ).hexdigest(),
                        "path": task.target_path,
                        "content_sha256": hashlib.sha256((task.content or "").encode()).hexdigest(),
                    },
                )
                self.repository.session.add(operation)
                self.repository.commit()
            operation.attempts += 1
            operation.status = "PENDING"
            self.repository.commit()

            # B5: a compound write→run task keeps its run argv on the SHELL STEP
            # (the task tool stays workspace.write_text so the named target path
            # survives). Step arguments win over the task-level command.
            step_command = self._step_command(typed)
            command = step_command or self._command(task)
            workspace_root = self._execution_workspace_root(task)

            # `step_content` is the body the write branch below would use; it is
            # bound here so no branch can read an unbound local.
            step_content = self._step_content(typed)
            if step_content is None:
                step_content = task.content

            # P0 false-DONE machine: a plan that declares NO side effect (an
            # answer-only request — conversation/answer, an unresolvable shell
            # command, a write whose content cannot be derived) must never
            # execute a tool. The dispatch below used to fall through to the
            # write branch for every unknown tool name, materializing an
            # artifact — that is how a message became a "completed side
            # effect". Fail closed instead: no artifact, and the failure reason
            # names the evidence that does not exist.
            if (
                (task.tool_arguments or {}).get("answer_only") is True
                or step_tool_name == ANSWER_ONLY_TOOL
            ):
                result = ToolResult(
                    False,
                    "failed",
                    error=(
                        "answer-only plan: no side effect requested; "
                        "no artifact exists to verify"
                    ),
                )
            elif step_tool_name == "send_email":
                result = self._execute_send_email(task)
                if result.ok:
                    initial_projection = project_tool_result(
                        result,
                        path=task.target_path,
                        command=(),
                        content=task.content,
                        workspace_root=workspace_root,
                    )
                    result = self._materialize_stdout_artifact(
                        task, result, initial_projection
                    )
            elif step_tool_name == "mcp":
                result = self._execute_mcp_tool(task)
                if result.ok and result.data.get("file"):
                    result = self._materialize_mcp_file_artifact(
                        task, result, workspace_root
                    )
                elif result.ok:
                    initial_projection = project_tool_result(
                        result,
                        path=task.target_path,
                        command=(),
                        content=task.content,
                        workspace_root=workspace_root,
                    )
                    result = self._materialize_stdout_artifact(
                        task, result, initial_projection
                    )
            elif step_tool_name == "sandbox.shell":
                if self.shell_tool is None:
                    result = ToolResult(False, "failed", error="shell tool unavailable")
                else:
                    try:
                        result = self.shell_tool.execute(ShellInput(command, operation_id))
                    except Exception:
                        result = ToolResult(False, "failed", error="tool execution failed")
                if result.ok:
                    initial_projection = project_tool_result(
                        result,
                        path=task.target_path,
                        command=command,
                        content=task.content,
                        workspace_root=workspace_root,
                    )
                    # B5: the run step of a write→run flow produces STDOUT, not
                    # the task's target file (which the previous step already
                    # wrote). Comparing the file against task.content here would
                    # discard the real program output.
                    if step_command or self._is_stdout_only_shell_request(task):
                        result = self._materialize_stdout_artifact(
                            task,
                            result,
                            initial_projection,
                            # Only the run step of a write→run compound (the
                            # task tool stays workspace.write_text) reports the
                            # exit code; plain shell tasks stay stdout-only.
                            include_exit_code=bool(step_command)
                            and task.tool_name != "sandbox.shell",
                        )
                    else:
                        try:
                            observed, digest = self.tool.read_and_hash(task.target_path)
                            # BUG ANT-003: shell commands (echo/printf) emit a
                            # trailing newline; exact comparison failed even when
                            # the file content matched the expected content.
                            # Compare stripped, keep the observed raw text.
                            if observed.strip() != (task.content or "").strip():
                                result = ToolResult(
                                    False,
                                    "failed",
                                    error="shell output did not match expected content",
                                )
                            else:
                                result = ToolResult(
                                    True,
                                    "completed",
                                    data=result.data,
                                    artifacts=[
                                        ArtifactResult(
                                            task.target_path,
                                            digest,
                                            len(observed.encode()),
                                        )
                                    ],
                                )
                        except (OSError, ValueError):
                            result = ToolResult(
                                False,
                                "failed",
                                error="shell artifact unavailable",
                            )
            elif step_tool_name == "workspace.read_text":
                read_tool = WorkspaceReadTextTool(workspace=workspace_root)
                try:
                    read_res = read_tool.execute(task.target_path)
                    if read_res.ok:
                        creator_tool: str | None = None
                        if any(
                            s.tool_name == "workspace.write_text"
                            or (s.input and s.input.get("tool_name") == "workspace.write_text")
                            for s in task.steps
                        ):
                            creator_tool = "workspace.write_text"
                        else:
                            try:
                                prev_step = self.repository.session.scalar(
                                    select(FlowStep.tool_name)
                                    .join(Artifact, FlowStep.id == Artifact.step_id)
                                    .where(
                                        Artifact.path == task.target_path,
                                        Artifact.task_id != task.id,
                                        FlowStep.tool_name != "",
                                        FlowStep.tool_name.is_not(None),
                                    )
                                    .order_by(Artifact.created_at.desc())
                                    .limit(1)
                                )
                                if prev_step and str(prev_step).strip():
                                    creator_tool = str(prev_step).strip()
                                else:
                                    prev_task_art = self.repository.session.scalar(
                                        select(TaskFlow.tool_name)
                                        .join(Artifact, TaskFlow.id == Artifact.task_id)
                                        .where(
                                            Artifact.path == task.target_path,
                                            Artifact.task_id != task.id,
                                            TaskFlow.tool_name != "",
                                            TaskFlow.tool_name.is_not(None),
                                        )
                                        .order_by(Artifact.created_at.desc())
                                        .limit(1)
                                    )
                                    if prev_task_art and str(prev_task_art).strip():
                                        creator_tool = str(prev_task_art).strip()
                                    else:
                                        prev_task = self.repository.session.scalar(
                                            select(TaskFlow.tool_name)
                                            .where(
                                                TaskFlow.target_path == task.target_path,
                                                TaskFlow.id != task.id,
                                                TaskFlow.status.in_((
                                                    TaskState.DONE.value,
                                                    TaskState.VERIFYING.value,
                                                    TaskState.OBSERVING.value,
                                                )),
                                                TaskFlow.tool_name != "",
                                                TaskFlow.tool_name.is_not(None),
                                            )
                                            .order_by(TaskFlow.created_at.desc())
                                            .limit(1)
                                        )
                                        if prev_task and str(prev_task).strip():
                                            creator_tool = str(prev_task).strip()
                            except Exception:
                                pass

                        data: dict[str, Any] = {
                            "output": read_res.content,
                            "content": read_res.content,
                            "path": read_res.path,
                            "tool_name": read_res.tool_name,
                        }
                        if creator_tool:
                            data["creator_tool"] = creator_tool

                        result = ToolResult(
                            True,
                            "completed",
                            data=data,
                            artifacts=[
                                ArtifactResult(
                                    task.target_path,
                                    read_res.sha256,
                                    len(read_res.content.encode("utf-8")),
                                )
                            ],
                        )
                    else:
                        result = ToolResult(
                            False,
                            "failed",
                            error=read_res.error or "read failed",
                        )
                except Exception as exc:
                    result = ToolResult(False, "failed", error=f"tool execution failed: {exc}")
            elif step_tool_name in WRITE_TOOL_NAMES:
                # Fail-closed guard (BUG ANT-002, P0 data loss): a write with
                # empty content must never truncate an existing non-empty file.
                # Read-intent tasks that degraded into write_text (empty content)
                # would otherwise destroy the user's data silently.
                _target_nonempty = False
                try:
                    _target_nonempty = bool(self.tool.read_and_hash(task.target_path)[0].strip())
                except (OSError, ValueError):
                    _target_nonempty = False
                step_content = self._step_content(typed)
                if step_content is None:
                    step_content = task.content
                if (
                    canonical_tool_name(step_tool_name) == "workspace.write_text"
                    and not (step_content or "").strip()
                    and _target_nonempty
                ):
                    result = ToolResult(
                        False,
                        "failed",
                        error=(
                            "refusing to overwrite existing non-empty file "
                            "with empty content (possible read-intent misrouted "
                            "to write_text)"
                        ),
                    )
                else:
                    try:
                        result = self.tool.execute(
                            WriteFileInput(path=task.target_path, content=step_content)
                        )
                    except Exception:
                        result = ToolResult(False, "failed", error="tool execution failed")
            else:
                # FP-L05d: the dispatch chain used to end in a bare ``else`` that
                # executed a workspace write for ANY tool name — an unknown or
                # aliased name (e.g. ``workspace.write``) produced an artifact and
                # walked past the verifier's self-write guard (which looks for
                # exactly ``workspace.write_text``). Unknown side-effect state is
                # fail-closed: no tool runs, no artifact exists.
                result = ToolResult(
                    False,
                    "failed",
                    error=(
                        f"unknown tool {step_tool_name!r}: refusing to execute "
                        "(fail closed); no artifact exists"
                    ),
                )

            projection = project_tool_result(
                result,
                path=task.target_path,
                command=command,
                content=(
                    step_content
                    if canonical_tool_name(step_tool_name) == "workspace.write_text"
                    else task.content
                ),
                workspace_root=workspace_root,
            )
            # FP-L23R: persist the faithful stdout trace of a materialized
            # stdout artifact next to the chat-oriented projection. The
            # projection flattens newlines by design (result_safety), so without
            # this the verifier's recorded effect facts would describe a glued
            # token while the artifact holds the real two-line output — two
            # contradictory accounts of one effect, which is what made the
            # judge's verdict unstable (FP-L23T).
            _faithful_stdout = (
                result.data.get("stdout_faithful")
                if isinstance(result.data, dict)
                else None
            )
            _step_output: dict[str, Any] = {
                "ok": bool(result.ok and result.artifacts),
                "side_effect_key": task.side_effect_key,
                "tool_result": projection,
            }
            _effect_stdout: str | None = (
                _faithful_stdout
                if isinstance(_faithful_stdout, str) and _faithful_stdout
                else None
            )
            if _effect_stdout is not None:
                _step_output["effect_stdout"] = _effect_stdout
            typed.output = _step_output
            _operation_result: dict[str, Any] = {"tool_result": projection}
            if _effect_stdout is not None:
                # Persist the faithful trace on the durable operation too, so a
                # recovered APPLIED operation replays the same effect facts.
                _operation_result["effect_stdout"] = _effect_stdout
            operation.result = _operation_result
            operation.status = "PENDING" if result.ok and result.artifacts else "FAILED"
            operation.updated_at = utcnow()
            self.repository.commit()

            if not result.ok or not result.artifacts:
                failure_reason = str(projection.get("failure_reason") or "tool execution failed")
                if result.status == "cancelled":
                    self.repository.session.rollback()
                    self.repository.session.expire_all()
                    refreshed = self.repository.get(task.id)
                    if TaskState(refreshed.status) is TaskState.CANCELLED:
                        return None
                    for s in refreshed.steps:
                        if s.status == StepState.RUNNING.value:
                            self.repository.transition_step(
                                refreshed,
                                s,
                                StepState.CANCELLED,
                                "tool cancelled",
                                "worker",
                                correlation,
                            )
                    self.repository.transition(
                        refreshed,
                        TaskState.CANCELLED,
                        "durable cancellation observed",
                        "worker",
                        correlation_id=correlation,
                    )
                    self.repository.commit()
                    return None
                typed.retries += 1
                if result.retryable and typed.retries <= self.max_retries:
                    self.repository.commit()
                    return self._recover_or_execute(task, typed, correlation)
                if failure_reason == "tool execution timed out":
                    target = TaskState.TIMEOUT
                elif failure_reason == "tool execution blocked by sandbox":
                    target = TaskState.BLOCKED
                else:
                    target = TaskState.FAILED
                self.repository.transition_step(
                    task,
                    typed,
                    StepState.FAILED,
                    failure_reason,
                    "sandbox",
                    correlation,
                )
                self.repository.transition(
                    task,
                    target,
                    failure_reason,
                    "orchestrator",
                    correlation_id=correlation,
                )
                self.repository.commit()
                return None

            produced = result.artifacts[0]
            operation.status = "APPLIED"
            _applied_result: dict[str, Any] = {
                "path": produced.path,
                "sha256": produced.sha256,
                "size": produced.size,
                "tool_result": projection,
            }
            if _effect_stdout is not None:
                _applied_result["effect_stdout"] = _effect_stdout
            operation.result = _applied_result
            operation.updated_at = utcnow()
            self.repository.commit()
            art = self._persist_artifact(
                task,
                typed,
                produced.path,
                produced.sha256,
                produced.size,
                correlation,
                "side effect completed",
                projection,
                effect_stdout=_effect_stdout,
            )
            last_artifact = art

        return last_artifact

    def _persist_artifact(
        self,
        task: TaskFlow,
        step: object,
        path: str,
        digest: str,
        size: int,
        correlation: str,
        reason: str,
        tool_result: dict[str, Any] | None,
        *,
        effect_stdout: str | None = None,
    ) -> Artifact:
        typed = step if isinstance(step, FlowStep) else task.steps[0]
        output: dict[str, Any] = {
            "ok": True,
            "side_effect_key": task.side_effect_key,
        }
        if tool_result is not None:
            output["tool_result"] = tool_result
        if effect_stdout:
            # FP-L23R: the faithful stdout trace of the materialized artifact.
            # ``tool_result.stdout_preview`` is the chat projection and keeps no
            # newlines, so the verifier reads this record for the real effect
            # facts instead of judging a glued token.
            output["effect_stdout"] = effect_stdout
        typed.output = output
        self.repository.transition_step(task, typed, StepState.COMPLETED, reason, "sandbox", correlation)
        artifact = Artifact(
            task_id=task.id,
            step_id=typed.id,
            path=path,
            sha256=digest,
            size=size,
            evidence=output,
        )
        task.artifacts.append(artifact)
        task.checkpoint = "tool_completed"
        self.repository.session.flush()
        return artifact

    def spawn_child_flow(
        self,
        parent_task: TaskFlow,
        goal: str,
        target_path: str,
        content: str,
        tool_name: str = "workspace.write_text",
        tool_arguments: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> TaskFlow:
        # Idempotency: a repeated spawn with an explicit key that already resolved
        # to a child of this owner returns that same child, never a duplicate.
        # The check runs before the depth/budget gates so a retry is cheap and does
        # not spuriously trip the budget limit on the row it already created.
        if idempotency_key:
            existing = self.repository.session.scalar(
                select(TaskFlow).where(
                    TaskFlow.owner_id == parent_task.owner_id,
                    TaskFlow.idempotency_key == idempotency_key,
                    TaskFlow.parent_id == parent_task.id,
                )
            )
            if existing is not None:
                return existing

        parent_depth = parent_task.depth or 0
        parent_max_depth = (
            parent_task.max_depth if parent_task.max_depth is not None else 3
        )
        if parent_depth + 1 > parent_max_depth:
            raise DepthLimitExceeded(
                f"Depth limit exceeded: current depth {parent_depth}, max allowed {parent_max_depth}"
            )

        parent_max_budget = (
            parent_task.max_child_budget if parent_task.max_child_budget is not None else 5
        )
        child_count = (
            self.repository.session.scalar(
                select(func.count()).select_from(TaskFlow).where(TaskFlow.parent_id == parent_task.id)
            )
            or 0
        )
        if child_count >= parent_max_budget:
            raise BudgetLimitExceeded(
                f"Child task budget exceeded: current count {child_count}, max allowed {parent_max_budget}"
            )

        child_id = str(uuid.uuid4())
        idem_key = (
            idempotency_key
            or f"child-{parent_task.id}-{child_count + 1}-{uuid.uuid4().hex[:8]}"
        )
        fingerprint = hashlib.sha256(
            f"{parent_task.owner_id}:{goal}:{target_path}:{content}".encode()
        ).hexdigest()

        child_task = TaskFlow(
            id=child_id,
            parent_id=parent_task.id,
            owner_id=parent_task.owner_id,
            goal=goal,
            target_path=target_path,
            content=content,
            tool_name=tool_name,
            tool_arguments=tool_arguments or {},
            depth=parent_depth + 1,
            max_depth=parent_max_depth,
            max_child_budget=parent_max_budget,
            payload_fingerprint=fingerprint,
            idempotency_key=idem_key,
            status=TaskState.RECEIVED.value,
        )
        step = FlowStep(
            task_id=child_id,
            index=0,
            title=f"Step 1: {goal}",
            input={
                "path": target_path,
                "content": content,
                "tool_name": tool_name,
                "tool_arguments": tool_arguments or {},
            },
            status=StepState.PENDING.value,
        )
        child_task.steps.append(step)
        self.repository.session.add(child_task)
        self.repository.commit()
        return child_task

    def aggregate_child_results(self, parent_task: TaskFlow) -> dict[str, Any]:
        children = self.repository.session.scalars(
            select(TaskFlow).where(TaskFlow.parent_id == parent_task.id).order_by(TaskFlow.created_at)
        ).all()
        results = []
        terminal_failures = {
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
            TaskState.BLOCKED.value,
            TaskState.POLICY_DENIED.value,
            TaskState.TIMEOUT.value,
        }
        done_count = 0
        failed_count = 0
        for child in children:
            if child.status == TaskState.DONE.value:
                done_count += 1
            elif child.status in terminal_failures:
                failed_count += 1
            results.append({
                "id": child.id,
                "goal": child.goal,
                "status": child.status,
                "target_path": child.target_path,
                "artifacts": [
                    {"id": a.id, "path": a.path, "sha256": a.sha256} for a in child.artifacts
                ],
            })
        all_done = len(children) > 0 and (done_count == len(children))
        return {
            "parent_id": parent_task.id,
            "total_children": len(children),
            "done_count": done_count,
            "failed_count": failed_count,
            "pending_count": len(children) - done_count - failed_count,
            "all_done": all_done,
            "children": results,
        }


def spawn_child_flow(
    orchestrator: Orchestrator,
    parent_task: TaskFlow,
    goal: str,
    target_path: str,
    content: str,
    tool_name: str = "workspace.write_text",
    tool_arguments: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> TaskFlow:
    return orchestrator.spawn_child_flow(
        parent_task=parent_task,
        goal=goal,
        target_path=target_path,
        content=content,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        idempotency_key=idempotency_key,
    )


def aggregate_child_results(
    orchestrator: Orchestrator,
    parent_task: TaskFlow,
) -> dict[str, Any]:
    return orchestrator.aggregate_child_results(parent_task=parent_task)
