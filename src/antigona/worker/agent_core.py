from __future__ import annotations

import hashlib
import importlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from antigona.config import Settings
from antigona.models import FlowStep, StepState, TaskFlow, TaskState
from antigona.repository import InvalidTransition, TaskRepository
from antigona.worker.hitl import RiskLevel
from antigona.worker.quarantine import QuarantineModel, QuarantineUnavailableError
from antigona.workspace import BaseWorkspace, WorkspaceFactory

from .tools import WebFetchTool

JsonObject = dict[str, Any]
EventCallback = Callable[["AgentEvent"], None]
ToolCallable = Callable[[JsonObject], JsonObject]


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    message: str
    payload: JsonObject


class ConversationBackend(Protocol):
    conversation_id: str

    def register_tool(self, name: str, handler: ToolCallable) -> None:
        ...

    def run(self, prompt: str, callbacks: Sequence[EventCallback]) -> None:
        ...


@dataclass(frozen=True)
class AgentCoreConfig:
    workspace: Path | BaseWorkspace
    persistence_dir: Path
    conversation_id: str
    sdk_agent: Any | None = None
    quarantine_model: str = "none"
    model_primary: str = "openrouter/anthropic/claude-3.5-sonnet"
    quarantine: Any | None = None
    egress_proxy: Any | None = None
    web_fetch_tool: Any | None = None
    settings: Settings | None = None


class SDKUnavailableError(RuntimeError):
    pass


class ScriptedConversation:
    def __init__(self, persistence_dir: Path, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self._tools: dict[str, ToolCallable] = {}
        self._state_path = persistence_dir / f"{conversation_id}.json"
        self._summary_path = persistence_dir / f"{conversation_id}.summary.json"
        persistence_dir.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists():
            self._state = json.loads(self._state_path.read_text(encoding="utf-8"))
        else:
            self._state = {"conversation_id": conversation_id, "turns": []}

    def register_tool(self, name: str, handler: ToolCallable) -> None:
        self._tools[name] = handler

    def run(self, prompt: str, callbacks: Sequence[EventCallback]) -> None:
        for callback in callbacks:
            callback(AgentEvent("planning", "prompt accepted", {"prompt": prompt}))
        payload = json.loads(prompt)
        if not isinstance(payload, dict):
            raise ValueError("prompt must decode to an object")
        tool_name = str(payload["tool"])
        raw_arguments = payload["arguments"]
        if not isinstance(raw_arguments, dict):
            raise ValueError("tool arguments must be an object")
        arguments = dict(raw_arguments)
        handler = self._tools[tool_name]
        for callback in callbacks:
            callback(AgentEvent("tool_call", f"dispatch {tool_name}", {"tool": tool_name, "arguments": arguments}))
        try:
            result = handler(arguments)
        except Exception as exc:
            error_result: JsonObject = {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "tool": tool_name,
                "arguments": arguments,
            }
            for callback in callbacks:
                callback(AgentEvent("tool_result", f"{tool_name} failed: {exc}", {"tool": tool_name, "result": error_result}))
                callback(AgentEvent("verifying", "error captured", {}))
            self._state["turns"].append({"prompt": payload, "result": error_result, "error": True})
            self._atomic_json_write(self._state_path, self._state)
            self._atomic_json_write(
                self._summary_path,
                {
                    "conversation_id": self.conversation_id,
                    "last_tool": tool_name,
                    "last_trust": "untrusted",
                    "turn_count": len(self._state["turns"]),
                },
            )
            return
        for callback in callbacks:
            callback(AgentEvent("tool_result", f"{tool_name} completed", {"tool": tool_name, "result": result}))
            callback(AgentEvent("verifying", "result captured", {}))
        self._state["turns"].append({"prompt": payload, "result": result})
        self._atomic_json_write(self._state_path, self._state)
        trust = "untrusted" if bool(result.get("untrusted", False)) else "trusted"
        self._atomic_json_write(
            self._summary_path,
            {
                "conversation_id": self.conversation_id,
                "last_tool": tool_name,
                "last_trust": trust,
                "turn_count": len(self._state["turns"]),
            },
        )

    @staticmethod
    def _atomic_json_write(path: Path, payload: object) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    @property
    def turns(self) -> list[JsonObject]:
        raw_turns = self._state.get("turns", [])
        if not isinstance(raw_turns, list):
            return []
        return [dict(turn) for turn in raw_turns if isinstance(turn, dict)]


class _OpenHandsEventCollector:
    def __init__(self) -> None:
        self.handler: Callable[[Any], None] | None = None

    def dispatch(self, event: Any) -> None:
        if self.handler is not None:
            self.handler(event)


class OpenHandsConversation:
    def __init__(
        self,
        conversation: Any,
        conversation_id: str,
        collector: _OpenHandsEventCollector,
        register_tool_fn: Callable[[str, Any], None] | None,
    ) -> None:
        self._conversation = conversation
        self.conversation_id = conversation_id
        self._collector = collector
        self._register_tool_fn = register_tool_fn
        self._active_callbacks: Sequence[EventCallback] = ()
        self._collector.handler = self._bridge

    def register_tool(self, name: str, handler: ToolCallable) -> None:
        if hasattr(self._conversation, "register_tool"):
            self._conversation.register_tool(name=name, func=handler)
            return
        if self._register_tool_fn is None:
            raise SDKUnavailableError("OpenHands SDK register_tool hook is unavailable")
        try:
            self._register_tool_fn(name, handler)
        except Exception as exc:
            raise SDKUnavailableError(
                "OpenHands register_tool rejected callable handler; provide a ToolDefinition bridge"
            ) from exc

    def _bridge(self, event: Any) -> None:
        kind = str(getattr(event, "type", "unknown"))
        message = str(getattr(event, "message", kind))
        payload: JsonObject = {"raw": repr(event)}
        for callback in self._active_callbacks:
            callback(AgentEvent(kind=kind, message=message, payload=payload))

    def run(self, prompt: str, callbacks: Sequence[EventCallback]) -> None:
        self._active_callbacks = callbacks
        if hasattr(self._conversation, "send_message"):
            self._conversation.send_message(prompt)
        self._conversation.run()


def build_local_conversation(config: AgentCoreConfig) -> ConversationBackend:
    try:
        sdk = importlib.import_module("openhands.sdk")
        local_cls = sdk.LocalConversation
        register_tool_fn = getattr(sdk, "register_tool", None)
        if config.sdk_agent is None:
            raise SDKUnavailableError("sdk_agent is required for LocalConversation")
        try:
            conversation_uuid = uuid.UUID(config.conversation_id)
        except ValueError:
            conversation_uuid = uuid.uuid5(uuid.NAMESPACE_URL, config.conversation_id)
        collector = _OpenHandsEventCollector()
        ws_path = str(config.workspace.root_path if isinstance(config.workspace, BaseWorkspace) else config.workspace)
        conversation = local_cls(
            agent=config.sdk_agent,
            workspace=ws_path,
            persistence_dir=str(config.persistence_dir),
            conversation_id=conversation_uuid,
            callbacks=[collector.dispatch],
            visualizer=None,
        )
    except Exception as exc:  # pragma: no cover - runtime availability branch
        raise SDKUnavailableError(str(exc)) from exc
    return OpenHandsConversation(
        conversation=conversation,
        conversation_id=config.conversation_id,
        collector=collector,
        register_tool_fn=register_tool_fn,
    )


class AgentEventProjector:
    def __init__(self, repository: TaskRepository, actor: str = "worker-agent") -> None:
        self.repository = repository
        self.actor = actor

    def project(self, task: TaskFlow, event: AgentEvent, correlation_id: str) -> None:
        from antigona.observability import event as log_event

        result = event.payload.get("result")
        result_untrusted = isinstance(result, dict) and bool(result.get("untrusted", False))
        trust = (
            "untrusted"
            if bool(event.payload.get("untrusted", False)) or result_untrusted
            else "trusted"
        )
        log_event(
            f"worker.agent.{event.kind}",
            service="worker",
            correlation_id=correlation_id,
            task_id=task.id,
            session_id=task.owner_id,
            step_id=next((step.id for step in task.steps if step.status == StepState.RUNNING.value), None),
            status="running",
            trust=trust,
        )
        mapping = {
            "planning": TaskState.PLANNING,
            "tool_call": TaskState.TOOL_EXECUTING,
            "tool_result": TaskState.OBSERVING,
            "verifying": TaskState.VERIFYING,
        }
        target = mapping.get(event.kind)
        if target is None:
            return
        try:
            self.repository.transition(
                task,
                target,
                reason=event.message,
                actor=self.actor,
                correlation_id=correlation_id,
            )
            self.repository.commit()
        except InvalidTransition:
            return


class WorkerAgentCore:
    def __init__(
        self,
        config: AgentCoreConfig,
        conversation: ConversationBackend,
        repository: TaskRepository,
    ) -> None:
        self.config = config
        self.conversation = conversation
        self.repository = repository
        if isinstance(config.workspace, BaseWorkspace):
            self.workspace: BaseWorkspace = config.workspace
        else:
            # DF-WO2-003-full (GROK GAP B): create_workspace must see the real
            # Settings so ownership_enabled is honoured via config, not just the
            # env var.  Fall back to env-consistent settings when none injected.
            # Aliased to dodge the later local ``from antigona.config import Settings``
            # shadow inside this method (ruff F823).
            from antigona.config import Settings as _EnvSettings

            _cfg = config.settings if config.settings is not None else _EnvSettings.from_env()
            self.workspace = WorkspaceFactory.create_workspace(
                workspace_dir=config.workspace, config=_cfg
            )
        if config.web_fetch_tool is not None:
            self.web_fetch_tool: Any = config.web_fetch_tool
        elif config.egress_proxy is not None:
            self.web_fetch_tool = WebFetchTool(proxy=config.egress_proxy)
        else:
            from antigona.config import Settings
            from antigona.egress import EgressProxy

            self.web_fetch_tool = WebFetchTool(proxy=EgressProxy.from_settings(Settings.from_env()))
        self.projector = AgentEventProjector(repository=repository)
        self.untrusted_context: bool = False
        if config.quarantine is not None:
            self.quarantine: QuarantineModel | None = config.quarantine
        else:
            self.quarantine = QuarantineModel(
                primary_model=config.model_primary,
                quarantine_model=config.quarantine_model,
            )
        self._trust_state_path: Path | None = None
        self._current_task: TaskFlow | None = None
        self._current_step: FlowStep | None = None
        self._register_tools()

    def _register_tools(self) -> None:
        self.conversation.register_tool("workspace.write_text", self._tool_write_text)
        self.conversation.register_tool("workspace.read_text", self._tool_read_text)
        self.conversation.register_tool("sandbox.shell", self._tool_shell)
        self.conversation.register_tool("web.fetch", self._tool_web_fetch)
        self.conversation.register_tool("send_file", self._tool_send_file)

    def _bind_trust_state(self, task: TaskFlow) -> None:
        identity = json.dumps(
            [task.owner_id, task.id, str(self.workspace.root_path.resolve())], separators=(",", ":")
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self._trust_state_path = self.config.persistence_dir / "security" / f"{digest}.json"
        try:
            state = json.loads(self._trust_state_path.read_text(encoding="utf-8"))
            self.untrusted_context = state.get("trust") == "untrusted"
        except FileNotFoundError:
            self.untrusted_context = False

    def _degrade_trust(self) -> None:
        self.untrusted_context = True
        if self._trust_state_path is None:
            return
        self._trust_state_path.parent.mkdir(parents=True, exist_ok=True)
        ScriptedConversation._atomic_json_write(
            self._trust_state_path,
            {"trust": "untrusted"},
        )

    def _tool_write_text(self, arguments: JsonObject) -> JsonObject:
        rel_path = str(arguments["path"])
        content_str = str(arguments["content"])
        
        # Reservation-Settlement CAS Idempotency Check for File Writes
        claim_key = None
        if self._current_task is not None and self._current_step is not None:
            task_id = str(self._current_task.id)
            step_idx = getattr(self._current_step, "sequence", 0) or 0
            if hasattr(self, "operation_store") and self.operation_store is not None:
                import asyncio
                import hashlib
                import json
                call_payload = json.dumps({"path": rel_path, "content": content_str}, sort_keys=True)
                call_hash = hashlib.sha256(call_payload.encode()).hexdigest()[:12]
                tool_call_id = str(arguments.get("tool_call_id", "")) or None
                try:
                    fut = self.operation_store.reserve_tool_execution(
                        task_id, "write_file", step_idx, tool_call_id=tool_call_id, call_hash=call_hash
                    )
                    try:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            status, claim_key = pool.submit(lambda: asyncio.run(fut)).result()
                    except RuntimeError:
                        status, claim_key = asyncio.run(fut)
                    
                    if status == "UNCERTAIN":
                        from antigona.worker.tools.common import ToolError
                        raise ToolError("idempotency_reservation_failed: unable to establish safe execution lock")
                    elif status == "COMPLETED":
                        read_res = self.workspace.read_file(path=rel_path)
                        return {
                            "path": read_res.path,
                            "content": read_res.content,
                            "sha256": read_res.sha256,
                            "untrusted": self.untrusted_context,
                            "skipped_duplicate": True,
                        }
                    elif status == "PENDING":
                        # File Reconciliation: If target file exists and hash matches expected, reconcile as completed
                        try:
                            read_res = self.workspace.read_file(path=rel_path)
                            expected_sha256 = hashlib.sha256(content_str.encode()).hexdigest()
                            if read_res.sha256 == expected_sha256:
                                return {
                                    "path": read_res.path,
                                    "content": read_res.content,
                                    "sha256": read_res.sha256,
                                    "untrusted": self.untrusted_context,
                                    "skipped_duplicate": True,
                                    "reconciled": True,
                                }
                        except Exception:
                            pass
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error("OperationStore reserve_tool_execution failed: %s", exc)
                    from antigona.worker.tools.common import ToolError
                    raise ToolError(f"idempotency_reservation_failed: {exc}") from exc

        result = self.workspace.write_file(path=rel_path, content=content_str)
        
        # Settle CAS reservation on completion
        if claim_key and hasattr(self, "operation_store") and self.operation_store is not None:
            import asyncio
            try:
                assert self._current_task is not None
                settle_fut = self.operation_store.settle_tool_execution(
                    str(self._current_task.id), claim_key, result={"sha256": result.sha256}
                )
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        pool.submit(lambda: asyncio.run(settle_fut)).result()
                except RuntimeError:
                    asyncio.run(settle_fut)
            except Exception:
                pass
        if self._current_task is not None and self._current_step is not None:
            from antigona.models import Artifact
            artifact = Artifact(
                task_id=self._current_task.id,
                step_id=self._current_step.id,
                path=result.path,
                sha256=result.sha256,
                size=len(result.content.encode()),
                evidence={"sha256": result.sha256, "untrusted": self.untrusted_context},
            )
            self.repository.session.add(artifact)
            self.repository.commit()
        return {
            "path": result.path,
            "content": result.content,
            "sha256": result.sha256,
            "untrusted": self.untrusted_context,
        }

    def _tool_read_text(self, arguments: JsonObject) -> JsonObject:
        is_untrusted = bool(arguments.get("untrusted", False))
        result = self.workspace.read_file(path=str(arguments["path"]), untrusted=is_untrusted)
        content_out = result.content
        if result.untrusted or is_untrusted:
            self._degrade_trust()
            if self.quarantine is not None:
                try:
                    sanitized = self.quarantine.sanitize(result.content)
                    content_out = sanitized.safe_facts
                    if sanitized.injection_detected or sanitized.degraded:
                        self._degrade_trust()
                except QuarantineUnavailableError:
                    self._degrade_trust()
                    content_out = "[QUARANTINE_UNAVAILABLE]"
        return {
            "path": result.path,
            "content": content_out,
            "sha256": result.sha256,
            "untrusted": self.untrusted_context,
        }

    def _tool_shell(self, arguments: JsonObject) -> JsonObject:
        if self.untrusted_context:
            from antigona.worker.tools.common import ToolError
            raise ToolError("shell tool disabled after reading untrusted content (OWASP Agentic Top 10)")
        raw_command = arguments["command"]
        if not isinstance(raw_command, list):
            raise TypeError("command must be a list of strings")
        command = [str(item) for item in raw_command]

        # Reservation-Settlement CAS Idempotency Check for Shell
        claim_key = None
        if self._current_task is not None and self._current_step is not None:
            task_id = str(self._current_task.id)
            step_idx = getattr(self._current_step, "sequence", 0) or 0
            if hasattr(self, "operation_store") and self.operation_store is not None:
                import asyncio
                import hashlib
                import json
                cmd_payload = json.dumps(command, sort_keys=True)
                cmd_hash = hashlib.sha256(cmd_payload.encode()).hexdigest()[:12]
                tool_call_id = str(arguments.get("tool_call_id", "")) or None
                try:
                    fut = self.operation_store.reserve_tool_execution(
                        task_id, "sandbox.shell", step_idx, tool_call_id=tool_call_id, call_hash=cmd_hash
                    )
                    try:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            status, claim_key = pool.submit(lambda: asyncio.run(fut)).result()
                    except RuntimeError:
                        status, claim_key = asyncio.run(fut)
                    
                    if status == "UNCERTAIN":
                        from antigona.worker.tools.common import ToolError
                        raise ToolError("idempotency_reservation_failed: unable to establish safe shell execution lock")
                    elif status == "COMPLETED":
                        return {
                            "command": command,
                            "exit_code": 0,
                            "stdout": "[CAS-SKIPPED] Duplicate shell execution skipped by idempotency policy",
                            "stderr": "",
                            "untrusted": self.untrusted_context,
                            "skipped_duplicate": True,
                        }
                    # If status is PENDING (crashed before settlement) or RESERVED (new), proceed to execute shell.
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error("OperationStore reserve_tool_execution failed for shell: %s", exc)
                    from antigona.worker.tools.common import ToolError
                    raise ToolError(f"idempotency_reservation_failed: {exc}") from exc

        # A high-risk shell command may only execute inside the isolated Docker
        # sandbox, and ONLY after explicit owner approval (the flow gates it via
        # request_approval -> decide_approval). Approval does not bypass the
        # policy gate — it merely permits transition to the sandbox execution
        # path. Low-risk allowlisted commands still run on the host unchanged.
        approved = self._has_approved_shell_approval()
        correlation_id = str(getattr(self._current_task, "correlation_id", "") or "")
        task_id = str(getattr(self._current_task, "id", "") or "")
        result = self.workspace.execute_command(
            command,
            approved=approved,
            correlation_id=correlation_id,
            task_id=task_id,
        )

        # Settle CAS reservation on completion
        if claim_key and hasattr(self, "operation_store") and self.operation_store is not None:
            import asyncio
            try:
                assert self._current_task is not None
                settle_fut = self.operation_store.settle_tool_execution(
                    str(self._current_task.id), claim_key, result={"exit_code": result.exit_code}
                )
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        pool.submit(lambda: asyncio.run(settle_fut)).result()
                except RuntimeError:
                    asyncio.run(settle_fut)
            except Exception:
                pass

        return {
            "command": list(result.command),
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "untrusted": self.untrusted_context,
        }

    def _has_approved_shell_approval(self) -> bool:
        """True if the current task holds an APPROVED approval for this shell call.

        Low-risk commands are auto-approved by the flow (should_auto_approve), so
        they reach the host path regardless — those approvals carry no grant and
        are not owner confirmations. An approval decided by the OWNER carries a
        durable one-shot grant (A-1): it is consumed here, so a single owner
        approval authorizes exactly ONE shell execution, not unlimited ones.
        """
        task = self._current_task
        if task is None:
            return False
        approvals = getattr(task, "approvals", None) or []
        tool_names = {str(getattr(task, "tool_name", "") or ""), "sandbox.shell"}
        for a in approvals:
            if str(getattr(a, "tool_name", "") or "") not in tool_names:
                continue
            if str(getattr(a, "decision", "") or "").upper() != "APPROVED":
                continue
            grant_token = str(getattr(a, "grant_token", "") or "")
            if not grant_token:
                # Deterministic auto-approval (no owner confirmation, no grant).
                return True
            if self._consume_approval_grant(a, grant_token):
                return True
        return False

    def _consume_approval_grant(self, approval: Any, grant_token: str) -> bool:
        """Verify and consume the owner's one-shot approval grant (fail-closed)."""
        from antigona.security.approval_grant import ApprovalGrantStore

        try:
            verdict = ApprovalGrantStore().verify_and_consume_stored(
                grant_token,
                actor=str(getattr(approval, "decided_by", "") or ""),
                tool_name=str(getattr(approval, "tool_name", "") or ""),
                args=dict(getattr(approval, "arguments", None) or {}),
                consumed_by=f"worker:{getattr(self._current_task, 'id', '')}",
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "approval grant verification failed (fail-closed)"
            )
            return False
        return bool(verdict.valid)

    def _tool_web_fetch(self, arguments: JsonObject) -> JsonObject:
        result = self.web_fetch_tool.fetch(url=str(arguments["url"]))
        self._degrade_trust()
        detail_or_raw = result.detail
        if self.quarantine is not None:
            try:
                sanitized = self.quarantine.sanitize(detail_or_raw)
                detail_out = sanitized.safe_facts
                if sanitized.injection_detected or sanitized.degraded:
                    self._degrade_trust()
            except QuarantineUnavailableError:
                self._degrade_trust()
                detail_out = "[QUARANTINE_UNAVAILABLE]"
        else:
            detail_out = detail_or_raw
        return {
            "url": result.url,
            "enabled": result.enabled,
            "detail": detail_out,
            "untrusted": self.untrusted_context,
        }


    def _tool_send_file(self, arguments: JsonObject) -> JsonObject:
        """Send a workspace file outbound to Telegram via the delivery adapter.

        Normal files: delivered (owner PIN-approved). Secret files (.pem/.key/.env)
        are refused unless explicitly confirmed — protects against exfiltration.
        """
        path = str(arguments.get("path", ""))
        caption = str(arguments.get("caption", ""))
        if self._current_task is not None:
            owner_id = getattr(self._current_task, "owner_id", "") or ""
            chat_id = str(owner_id)
        else:
            chat_id = os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID", "")
        from antigona.delivery.adapter import TelegramAdapter
        from antigona.worker.hitl import evaluate_risk
        risk, _reason = evaluate_risk("send_file", {"path": path})
        if risk == RiskLevel.HIGH:
            return {
                "ok": False,
                "blocked": True,
                "reason": "Secret file (.pem/.key/.env) requires explicit owner confirmation; refusing to send outbound.",
            }
        adapter = TelegramAdapter(
            bot_token=os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id=chat_id or os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID"),
            mock=os.environ.get("ANTIGONA_DELIVERY_MOCK", "0") in ("1", "true", "True"),
        )
        result = adapter.send_file(path=path, caption=caption)
        return {"ok": True, "delivered": result, "path": path, "untrusted": self.untrusted_context}


    def run_turn(self, task: TaskFlow, step: FlowStep, prompt: str, correlation_id: str) -> None:
        from antigona.observability import event as log_event

        self._current_task = task
        self._current_step = step
        self._bind_trust_state(task)
        try:
            self.repository.transition_step(
                task,
                step,
                target=StepState.RUNNING,
                reason="agent turn started",
                actor="worker",
                correlation_id=correlation_id,
            )
            self.repository.commit()
            self.conversation.run(
                prompt=prompt,
                callbacks=[lambda event: self.projector.project(task, event, correlation_id)],
            )
            self.repository.transition_step(
                task,
                step,
                target=StepState.COMPLETED,
                reason="agent turn completed",
                actor="worker",
                correlation_id=correlation_id,
            )
            self.repository.commit()
            log_event(
                "worker.turn.completed",
                service="worker",
                correlation_id=correlation_id,
                task_id=task.id,
                session_id=task.owner_id,
                step_id=step.id,
                status="completed",
            )
        except Exception as exc:
            log_event(
                "worker.turn.failed",
                service="worker",
                correlation_id=correlation_id,
                task_id=task.id,
                session_id=task.owner_id,
                step_id=step.id,
                status="error",
                error_type=type(exc).__name__,
            )
            raise
        finally:
            self._current_task = None
            self._current_step = None


def create_worker_agent_core(
    config: AgentCoreConfig,
    repository: TaskRepository,
    *,
    force_scripted: bool = False,
) -> WorkerAgentCore:
    conversation: ConversationBackend
    if force_scripted:
        conversation = ScriptedConversation(config.persistence_dir, config.conversation_id)
    else:
        try:
            conversation = build_local_conversation(config)
        except SDKUnavailableError:
            conversation = ScriptedConversation(config.persistence_dir, config.conversation_id)
    return WorkerAgentCore(config=config, conversation=conversation, repository=repository)
