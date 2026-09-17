"""Gateway REST + WebSocket API.

Endpoints (P0.1):
- POST /flows                     create a task_flow and enqueue it (state machine only)
- GET  /flows                     list the caller's flows (read-only, P2.4)
- GET  /flows/{flow_id}           read flow state
- GET  /approvals                 list the caller's approvals (read-only, P2.4)
- GET  /approvals/{approval_id}   read one approval (read-only, P2.4)
- POST /flows/{flow_id}/cancel    sticky cancel
- POST /approvals/{approval_id}/decision   decide a pending approval
- WS   /flows/{flow_id}/progress  stream state transitions (each event carries correlation_id)

The gateway only enqueues work; it never executes tasks. There is
deliberately NO endpoint or helper here that can finalize a flow: DONE
is reserved for the Verifier process (repository raises on DONE too).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..conversation.dialogue_engine import DialogueEngine
from ..core.brain import AntigonaBrain, ResponseType
from ..core.task_backend import GatewayTaskBackend
from ..core.task_service import TaskSubmissionService
from ..database import Database
from ..models import Approval, StateTransition, TaskFlow, TaskState
from ..queue import DurableQueue
from ..repository import (
    ConcurrentUpdate,
    IdempotencyConflict,
    InvalidTransition,
    SensitiveTaskInput,
    TaskNotFound,
    TaskRepository,
)
from ..result_safety import (
    is_safe_workspace_path,
    is_sensitive_execution,
    sanitize_failure_reason,
    sanitize_result_text,
)
from ..schemas import (
    ApprovalDecision,
    ApprovalListEntry,
    ApprovalListView,
    ApprovalView,
    DialogueTurnRequest,
    DialogueTurnResponse,
    EventView,
    FlowListView,
    FlowResultView,
    FlowSummary,
    ReplayResponse,
    ScheduleCreate,
    ScheduleView,
    SteerFlowRequest,
    TaskCreate,
    TaskSubmit,
    TaskView,
    TimelineEntry,
    TimelineResponse,
    VerifiedArtifactResultView,
)
from ..skills import SkillsRegistry
from ..task_goal import requires_exact_write_read_contract
from .correlation import CORRELATION_HEADER, ensure_correlation_id, log_event

TERMINAL_STATES = {
    TaskState.DONE.value,
    TaskState.FAILED.value,
    TaskState.BLOCKED.value,
    TaskState.CANCELLED.value,
    TaskState.TIMEOUT.value,
    TaskState.POLICY_DENIED.value,
}

def _sandbox_isolation() -> Any:
    """Probe (LIVE) the isolation level sandboxed commands would actually get.

    Reflective, not configured: the runtime list is read from the Docker daemon
    on every call, so an external check detects a downgrade (e.g. ``runsc``
    unregistered) even when no configuration changed.  Also persists the result
    to the runtime state root so it is observable without speaking HTTP.
    """
    from antigona.sandbox.runner import probe_isolation, write_isolation_state

    status = probe_isolation()
    write_isolation_state(status)
    return status


_CRON_TICK_TASK: asyncio.Task[None] | None = None

# Upper bound for ?limit= on the read-only list endpoints, so a client cannot
# turn a listing into a full-table dump.
MAX_PAGE_SIZE = 200


def create_gateway_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    database = Database(resolved.database_url)
    token_hashes = resolved.token_hashes()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        global _CRON_TICK_TASK
        database.create_all()

        # ── Единое серверное ядро (Stage 1) ────────────────────────────────
        # Один AntigonaBrain на серверный runtime. DialogueEngine/SessionRepository
        # создаются здесь и внедряются в brain; CLI/Telegram НЕ создают свои
        # экземпляры — они ходят в это ядро через /api/v1/dialogue/turn.
        from ..sessions.repository import SessionRepository
        from ..tools.registry import ToolRegistry, register_builtins

        _registry = ToolRegistry()
        register_builtins(_registry)
        dialogue_engine = DialogueEngine(database=database, registry=_registry)
        session_repo = SessionRepository()
        brain = AntigonaBrain(
            dialogue_engine=dialogue_engine,
            session_repository=session_repo,
            task_backend=GatewayTaskBackend(database),
        )
        await brain.connect()
        app.state.brain = brain

        # Cron auto-tick. Opt-in via ANTIGONA_CRON_ENABLED (default off) so
        # enabling scheduling is a deliberate act, not a side effect of deploy.
        # The manual POST /schedules/tick path is unaffected either way.
        tick_interval = resolved.cron_tick_interval_seconds

        async def _background_tick() -> None:
            cron_log = logging.getLogger("antigona.gateway.cron")
            while True:
                try:
                    await asyncio.sleep(tick_interval)
                    if not resolved.cron_enabled:
                        continue

                    from ..cron import CronScheduler

                    def _tick() -> int:
                        # tick() is synchronous and does DB IO, so it must not
                        # run on the event loop; it also needs its own session
                        # rather than the request-scoped get_session dependency.
                        with database.session_factory() as session:
                            return len(CronScheduler(session).tick(
                                correlation_id="cron-autotick"
                            ))

                    created = await asyncio.to_thread(_tick)
                    if created:
                        cron_log.info("cron autotick created %d task(s)", created)
                except asyncio.CancelledError:
                    break
                except Exception:
                    # Never let a scheduling failure kill the loop, but do not
                    # swallow it silently either.
                    cron_log.exception("cron autotick failed")

        task = asyncio.create_task(_background_tick())
        _CRON_TICK_TASK = task
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await brain.close()

    app = FastAPI(title="Antigona Gateway", version="0.3.0", lifespan=lifespan)
    app.state.database = database

    @app.middleware("http")
    async def observe_http_failures(request: Request, call_next: Any) -> Response:
        cid = ensure_correlation_id(request.headers.get(CORRELATION_HEADER))
        request.state.correlation_id = cid
        response = cast(Response, await call_next(request))
        response.headers[CORRELATION_HEADER] = cid
        if response.status_code >= 400:
            path = request.url.path
            if response.status_code == 401:
                name, reason = "gateway.authentication_failed", "authentication_denied"
            elif response.status_code == 404 and path.startswith("/approvals/"):
                name, reason = "gateway.approval_not_found", "not_found"
            elif response.status_code == 404:
                name, reason = "gateway.flow_not_found", "not_found"
            elif response.status_code == 409 and path == "/flows":
                name, reason = "gateway.idempotency_conflict", "conflict"
            elif response.status_code == 409 and path.endswith("/cancel"):
                name, reason = "gateway.cancellation_failed", "invalid_transition"
            elif response.status_code == 409 and path.startswith("/approvals/"):
                name, reason = "gateway.approval_failed", "invalid_decision"
            else:
                name, reason = "gateway.request_failed", "invalid_request"
            log_event(
                name,
                cid,
                task_id=request.path_params.get("flow_id"),
                session_id=getattr(request.state, "owner_id", None),
                step_id=None,
                status=str(response.status_code),
                reason=reason,
            )
        return response

    def get_session() -> Iterator[Session]:
        yield from database.session()

    def correlation(request: Request, response: Response) -> str:
        cid = str(request.state.correlation_id)
        response.headers[CORRELATION_HEADER] = cid
        return cid

    def owner(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer token required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for known, value in token_hashes.items():
            if hmac.compare_digest(digest, known):
                request.state.owner_id = value
                return value
        raise HTTPException(401, "invalid bearer token")

    def load(
        flow_id: str,
        owner_id: str,
        session: Session,
    ) -> tuple[TaskRepository, TaskFlow]:
        repository = TaskRepository(session)
        try:
            return repository, repository.get(flow_id, owner_id)
        except TaskNotFound as exc:
            raise HTTPException(404, "flow not found") from exc

    def project_result(task: TaskFlow) -> FlowResultView:
        """Build a read-only, fail-closed terminal result projection."""

        terminal = task.status in TERMINAL_STATES
        if not terminal:
            return FlowResultView(
                flow_id=task.id,
                status=task.status,
                terminal=False,
                success=False,
                revision=task.revision,
            )

        command = task.tool_arguments.get("command") if task.tool_arguments else None
        blocked = is_sensitive_execution(task.target_path, command)
        stdout_preview: str | None = None
        read_back_summary: dict[str, object] | None = None
        for step in reversed(task.steps):
            output = step.output if isinstance(step.output, dict) else {}
            summary = output.get("tool_result")
            if not isinstance(summary, dict):
                continue
            if bool(summary.get("blocked")):
                blocked = True
                break
            if summary.get("tool_name") == "workspace.read_text" and read_back_summary is None:
                read_back_summary = summary
            candidate = sanitize_result_text(
                summary.get("stdout_preview"),
                preserve_newlines=summary.get("tool_name") == "workspace.read_text",
            )
            if candidate is not None and stdout_preview is None:
                stdout_preview = candidate

        artifacts: list[VerifiedArtifactResultView] = []
        safe_result_text: str | None = None
        if not blocked and task.status == TaskState.DONE.value:
            for artifact in sorted(task.artifacts, key=lambda item: (item.created_at, item.id)):
                if not artifact.verified:
                    continue
                if not is_safe_workspace_path(artifact.path, resolved.workspace):
                    continue
                artifacts.append(
                    VerifiedArtifactResultView(
                        path=artifact.path,
                        sha256=artifact.sha256,
                        size=artifact.size,
                        verified=True,
                    )
                )
                evidence = artifact.evidence if isinstance(artifact.evidence, dict) else {}
                read_back = sanitize_result_text(evidence.get("read_back"), preserve_newlines=True)
                if safe_result_text is None and read_back is not None:
                    safe_result_text = read_back

            if read_back_summary is not None:
                read_content = sanitize_result_text(
                    read_back_summary.get("stdout_preview"), preserve_newlines=True
                )
                read_path = sanitize_result_text(read_back_summary.get("path"), escape_html=False)
                read_tool = sanitize_result_text(read_back_summary.get("tool_name"), escape_html=False)
                write_tool = sanitize_result_text(
                    read_back_summary.get("creator_tool") or task.tool_name, escape_html=False
                )
                if read_content and read_path and read_tool and write_tool:
                    safe_result_text = (
                        f"Path: {read_path}\n"
                        f"Write tool: {write_tool}\n"
                        f"Read tool: {read_tool}\n"
                        "Write→read evidence: verified by read-back\n"
                        f"Content:\n{read_content}"
                    )

        requires_read_back = requires_exact_write_read_contract(
            task.goal, content=task.content, tool_name=task.tool_name
        )
        has_read_back_evidence = read_back_summary is not None and safe_result_text is not None
        success = (
            task.status == TaskState.DONE.value
            and not blocked
            and bool(artifacts)
            and (not requires_read_back or has_read_back_evidence)
        )
        failure_reason: str | None = None
        if blocked:
            failure_reason = "result withheld by safety policy"
            artifacts = []
            safe_result_text = None
            stdout_preview = None
        elif task.status != TaskState.DONE.value:
            stdout_preview = None
            matching_reason = next(
                (
                    transition.reason
                    for transition in reversed(task.transitions)
                    if transition.entity_type == "task" and transition.to_state == task.status
                ),
                None,
            )
            failure_reason = sanitize_failure_reason(matching_reason) or {
                TaskState.FAILED.value: "flow failed",
                TaskState.BLOCKED.value: "flow blocked",
                TaskState.CANCELLED.value: "flow cancelled",
                TaskState.TIMEOUT.value: "flow timed out",
                TaskState.POLICY_DENIED.value: "flow denied by policy",
            }.get(task.status, "flow did not complete")
        elif not artifacts:
            stdout_preview = None
            failure_reason = "verified result unavailable"
        elif requires_read_back and not has_read_back_evidence:
            stdout_preview = None
            safe_result_text = None
            failure_reason = "read-back evidence unavailable"

        return FlowResultView(
            flow_id=task.id,
            status=task.status,
            terminal=True,
            success=success,
            artifacts=artifacts,
            safe_result_text=safe_result_text,
            stdout_preview=stdout_preview,
            failure_reason=failure_reason,
            completed_at=task.updated_at,
            revision=task.revision,
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        isolation = _sandbox_isolation()
        return {
            "status": "ok",
            "isolation": isolation.level,
            "sandbox": isolation.as_dict(),
        }

    @app.get("/status")
    async def status_aggregate() -> dict[str, object]:
        """Aggregate stack health/readiness (Milestone 0, scope 4).

        Reports liveness of every critical service in one call:
        gateway (self), verifier (HTTP probe), worker + delivery (heartbeat).
        A service is ``up`` only when it answers within its window; anything
        unknown or stale is reported down — never assumed up.
        """
        from antigona.health.heartbeat import read_status

        verifier_url = os.getenv("ANTIGONA_VERIFIER_URL", "http://127.0.0.1:8091")
        verifier_up = False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{verifier_url}/health")
                verifier_up = r.status_code == 200
        except Exception:
            verifier_up = False

        heartbeats = read_status()
        isolation = _sandbox_isolation()
        return {
            "status": "ok",
            "service": "gateway",
            "services": {
                "gateway": {"up": True},
                "verifier": {"up": verifier_up},
                "worker": {"up": bool(heartbeats.get("worker", {}).get("up"))},
                "delivery": {"up": bool(heartbeats.get("delivery", {}).get("up"))},
            },
            "isolation": isolation.as_dict(),
        }

    @app.get("/api/status")
    def api_status() -> dict[str, str]:
        return {"status": "running", "service": "gateway", "version": "single-core-v1"}

    # ── Auth endpoints (OTP + server-side session) ────────────────────────

    def _get_bot_token() -> str:
        """Read TELEGRAM_BOT_TOKEN from environment."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise HTTPException(503, "TELEGRAM_BOT_TOKEN not configured")
        return token

    @app.post("/api/auth/send-otp")
    async def gw_send_otp(body: dict[str, Any]) -> dict[str, Any]:
        """Generate OTP and send via Telegram."""
        from antigona.security.otp import OTPManager
        from antigona.security.owner_identity import OwnerIdentity

        raw_telegram_id = body.get("telegram_id", 0)
        if not raw_telegram_id:
            raise HTTPException(422, "telegram_id required")
        try:
            telegram_id = int(str(raw_telegram_id).strip())
        except (TypeError, ValueError):
            raise HTTPException(422, "telegram_id must be an integer") from None

        # Проверяем, что telegram_id совпадает с владельцем. Без заданного
        # ANTIGONA_OWNER_ID владелец не сконфигурирован — fail-closed: код не
        # отправляется никуда, иначе OTP ушёл бы на присланный атакующим chat_id.
        identity = OwnerIdentity()
        owner_id = identity.owner_user_id
        if owner_id is None or telegram_id != owner_id:
            # Не раскрываем, что ID не совпал (security through obscurity)
            return {"sent_to": "telegram", "success": True, "message": "Если ID верный, код отправлен в Telegram"}

        owner_chat_id = owner_id
        bot_token = _get_bot_token()
        if not bot_token:
            return {"success": False, "message": "Telegram bot не настроен"}

        otp = OTPManager()
        await otp.create_dashboard_challenge(
            user_id=telegram_id,
            action_type="dashboard.login",
            bot_token=bot_token,
            chat_id=owner_chat_id,
        )

        return {
            "sent_to": "telegram",
            "success": True,
            "message": "Код отправлен в Telegram",
        }

    @app.post("/api/auth/login")
    async def gw_login(body: dict[str, Any]) -> dict[str, Any]:
        """Verify OTP code and return session token."""
        from antigona.security.otp import OTPManager

        telegram_id = body.get("telegram_id", 0)
        otp_code = body.get("otp_code", "")
        if not telegram_id or not otp_code:
            raise HTTPException(422, "telegram_id and otp_code required")

        otp = OTPManager()
        is_valid, result = await otp.verify_dashboard_code(
            user_id=telegram_id,
            action_type="dashboard.login",
            code_attempt=otp_code,
        )

        if not is_valid:
            return {"success": False, "message": result}

        # Создаём сессию
        from .auth_middleware import create_session

        token = await create_session(telegram_id)
        return {
            "success": True,
            "token": token,
            "telegram_id": telegram_id,
            "message": "Вход выполнен успешно",
        }

    @app.get("/api/auth/session/{token}")
    async def gw_check_session(token: str) -> dict[str, Any]:
        """Validate session token."""
        from .auth_middleware import get_session

        session = await get_session(token)
        if session is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "telegram_id": session["telegram_id"],
            "created_at": session["created_at"],
        }

    @app.post("/flows", response_model=TaskView, status_code=201)
    def create_flow(
        body: TaskCreate,
        response: Response,
        idempotency_key: Annotated[str, Header(min_length=1, alias="Idempotency-Key")],
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TaskView:
        if body.tool_name == "sandbox.shell" and not body.command:
            raise HTTPException(422, "shell command is required")
        if body.tool_name == "mcp" and not (body.mcp_server and body.mcp_tool):
            raise HTTPException(422, "mcp_server and mcp_tool are required for mcp tasks")
        service = TaskSubmissionService(database)
        try:
            result = service.submit(
                owner_id=owner_id,
                message=body.goal,
                idempotency_key=idempotency_key,
                tool_name=body.tool_name,
                path=body.path,
                command=tuple(body.command),
                content=body.content,
                read_after_write=body.read_after_write,
                run_after_write=body.run_after_write,
                run_command=tuple(body.run_command),
                fix_after_run=body.fix_after_run,
                fix_content=body.fix_content,
                fix_command=tuple(body.fix_command),
                mcp_server=body.mcp_server,
                mcp_tool=body.mcp_tool,
                mcp_arguments=body.mcp_arguments,
                params=body.params,
                correlation_id=correlation_id,
                client="api",
            )
        except SensitiveTaskInput as exc:
            raise HTTPException(422, "task input rejected by safety policy") from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result.get("created", True):
            response.status_code = 200
        return TaskView.model_validate(TaskRepository(session).get(result["flow_id"]))

    @app.post("/tasks", response_model=TaskView, status_code=201)
    def create_task(
        body: TaskSubmit,
        response: Response,
        idempotency_key: Annotated[str, Header(min_length=1, alias="Idempotency-Key")],
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TaskView:
        """Submit a free-text task. The message is used as the task goal.
        Thin adapter over the single TaskSubmissionService — the same service
        AntigonaBrain and POST /flows use (no separate orchestration path)."""
        service = TaskSubmissionService(database)
        try:
            result = service.submit(
                owner_id=owner_id,
                message=body.message,
                idempotency_key=idempotency_key,
                tool_name="workspace.write_text",
                path="task_output.txt",
                command=(),
                correlation_id=correlation_id,
                client="api",
            )
        except SensitiveTaskInput as exc:
            raise HTTPException(422, "task input rejected by safety policy") from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result.get("created", True):
            response.status_code = 200
        return TaskView.model_validate(TaskRepository(session).get(result["flow_id"]))

    @app.post("/api/v1/dialogue/turn", response_model=DialogueTurnResponse)
    async def dialogue_turn(
        request: DialogueTurnRequest,
        http_request: Request,
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> DialogueTurnResponse:
        """Canonical dialogue turn endpoint.

        Single server core: routes through the one AntigonaBrain instance created
        in the gateway lifespan. Thin clients (CLI/Telegram) never construct their
        own DialogueEngine/IntentRouter — they call this endpoint only.

        Auth: owner-scoped like every other gateway endpoint. Tasks created from
        a turn belong to the authenticated owner, so the client can live-wait on
        them through the normal owner-scoped flow endpoints.
        """
        brain: AntigonaBrain | None = getattr(app.state, "brain", None)
        if brain is None:
            # Fail closed: no brain in this server runtime — never spawn a
            # second core on the fly.
            return DialogueTurnResponse(
                reply="Ядро Antigona не инициализировано в этом сервере.",
                session_id=request.session_id,
                verified=None,
                response_type=ResponseType.ERROR,
            )
        turn_id = (request.turn_id or http_request.headers.get("X-Turn-Id") or correlation_id).strip()
        result = await brain.process(
            text=request.text,
            user_id=request.user_id,
            channel=request.channel,
            session_id=request.session_id,
            context={"owner_id": owner_id, "correlation_id": correlation_id, "turn_id": turn_id},
        )
        # verified semantics: only a Verifier-confirmed final task result may
        # set task_verified. Plain conversation/clarification/control replies
        # are NOT verified (verified=None). task_accepted is not a verified
        # result either — it is an acceptance, verification happens later.
        # Verified semantics: ONLY a real TASK_RESULT with task_verified=True
        # is verified. Everything else (conversation, clarification, control,
        # error, task_accepted — an acceptance, verification happens later)
        # has verified=None ("not applicable"), never False ("checked and
        # failed"). This is the Stage 1 contract: ordinary replies and task
        # acceptance are NOT verified results.
        task_verified = False
        if result.response_type == ResponseType.TASK_RESULT:
            task_verified = bool(result.metadata.get("task_verified"))
        verified: bool | None = None
        if result.response_type == ResponseType.TASK_RESULT:
            verified = task_verified
        # Truth contract: project the REAL tool/step outcome of the turn so a thin
        # client can never render a success header for a denied/failed/partial
        # tool run.  Only known outcome tokens are accepted; anything else is None
        # (not applicable).  last_error is bounded here and sanitized by the
        # client before it is shown in user-visible chat text.
        _known_outcomes = {"SUCCEEDED", "PARTIAL", "FAILED", "DENIED"}
        tool_outcome: str | None = None
        last_error: str | None = None
        result_meta = getattr(result, "metadata", None)
        if isinstance(result_meta, dict):
            raw_outcome = result_meta.get("tool_outcome")
            if isinstance(raw_outcome, str) and raw_outcome.strip().upper() in _known_outcomes:
                tool_outcome = raw_outcome.strip().upper()
            raw_error = result_meta.get("last_error")
            if isinstance(raw_error, str) and raw_error.strip():
                last_error = raw_error.strip()[:500]
        return DialogueTurnResponse(
            reply=result.text,
            session_id=request.session_id,
            verified=verified,
            response_verified=verified is True,
            task_verified=task_verified,
            response_type=result.response_type,
            flow_id=result.flow_id,
            requires_approval=result.requires_approval,
            tool_outcome=tool_outcome,
            last_error=last_error,
        )

    @app.get("/commands", response_model=list[dict[str, object]])
    def command_list(
        owner_id: Annotated[str, Depends(owner)],
        channel: str = "all",
    ) -> list[dict[str, object]]:
        """Единый реестр команд (Step 10 манифеста) — один источник для CLI и Telegram."""
        from ..core.command_registry import registry_payload

        if channel in {"cli", "telegram"}:
            return registry_payload(channel)
        return registry_payload("cli")

    def _owned_session(
        session_id: str,
        owner_id: str,
    ) -> str:
        """Проверить, что сессия принадлежит владельцу (защита от IDOR)."""
        if session_id == owner_id or session_id.endswith(f":{owner_id}"):
            return session_id
        raise HTTPException(404, "session not found")

    @app.get("/sessions/{session_id}")
    async def session_info(
        session_id: str,
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        """Session info — единая система сессий ядра (Step 12)."""
        brain: AntigonaBrain | None = getattr(app.state, "brain", None)
        if brain is None:
            raise HTTPException(503, "core not initialized")
        sid = _owned_session(session_id, owner_id)
        session = await brain._session_repo.get_session(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        messages = await brain._session_repo.get_messages(sid, limit=50)
        task_refs = await brain._session_repo.get_task_refs(sid, limit=50)
        return {
            "session": session,
            "message_count": len(messages),
            "task_refs": task_refs,
        }

    @app.post("/sessions/{session_id}/reset")
    async def session_reset(
        session_id: str,
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        """End the caller's conversation for this session (Task 1 fix).

        Clears message history and any tracked active flow for the session
        so the next turn starts with a clean slate. This is a per-session
        reset, not a process exit — a shared bot serving many chats must
        never terminate the whole process for a single user's ``/exit``.
        """
        brain: AntigonaBrain | None = getattr(app.state, "brain", None)
        if brain is None:
            raise HTTPException(503, "core not initialized")
        sid = _owned_session(session_id, owner_id)
        brain.set_active_flow(sid, None)
        deleted = await brain._session_repo.delete_session(sid)
        log_event(
            "gateway.session_reset",
            correlation_id,
            task_id=None,
            session_id=sid,
            step_id=None,
            status="reset",
            deleted=deleted,
        )
        return {"session_id": sid, "reset": True}

    @app.get("/sessions/{session_id}/history")
    async def session_history(
        session_id: str,
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
        limit: int = 100,
    ) -> dict[str, Any]:
        """Conversation history сессии (Step 12)."""
        brain: AntigonaBrain | None = getattr(app.state, "brain", None)
        if brain is None:
            raise HTTPException(503, "core not initialized")
        sid = _owned_session(session_id, owner_id)
        messages = await brain._session_repo.get_messages(sid, limit=max(1, min(limit, 200)))
        return {"session_id": sid, "messages": messages}

    # ── Единая память (Step 5-6 манифеста) ────────────────────────────────
    # Только ядро пишет в память; CLI/Telegram работают через этот API.

    @app.get("/api/v1/memory")
    def memory_list(
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from ..core.memory_repository import MemoryRepository

        repo = MemoryRepository(database)
        if query:
            items = repo.search(owner_id, query, limit=limit)
        else:
            items = repo.list_entries(owner_id, kind=kind, limit=limit)
        return {"items": items, "count": len(items)}

    @app.post("/api/v1/memory")
    def memory_create(
        request: dict[str, Any],
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        from ..core.memory_repository import MemoryRepository

        content = str(request.get("content") or "").strip()
        if not content:
            raise HTTPException(422, "content is required")
        repo = MemoryRepository(database)
        entry = repo.remember(
            owner_id,
            content,
            kind=str(request.get("kind") or "fact"),
            title=str(request.get("title") or ""),
            source=str(request.get("source") or "user"),
            task_id=request.get("task_id"),
        )
        return {"created": True, "entry": entry}

    @app.delete("/api/v1/memory/{entry_id}")
    def memory_delete(
        entry_id: str,
        owner_id: Annotated[str, Depends(owner)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        from ..core.memory_repository import MemoryRepository

        repo = MemoryRepository(database)
        deleted = repo.forget(owner_id, entry_id)
        if not deleted:
            raise HTTPException(404, "memory entry not found")
        return {"deleted": True}

    @app.get("/flows", response_model=FlowListView)
    def list_flows(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> FlowListView:
        # Read-only projection for the TUI: SELECT only, owner-scoped, no
        # state machine involvement and no path that could finalize a flow.
        page = max(1, min(limit, MAX_PAGE_SIZE))
        start = max(0, offset)
        scope = [TaskFlow.owner_id == owner_id]
        if status:
            scope.append(TaskFlow.status == status)
        total = session.scalar(select(func.count()).select_from(TaskFlow).where(*scope)) or 0
        rows = session.scalars(
            select(TaskFlow)
            .where(*scope)
            .order_by(TaskFlow.created_at.desc(), TaskFlow.id)
            .limit(page)
            .offset(start)
        ).all()
        log_event(
            "gateway.flows_listed", correlation_id, task_id=None, session_id=owner_id,
            step_id=None, status=status or "ALL", count=len(rows), total=int(total),
        )
        return FlowListView(
            items=[FlowSummary.model_validate(row) for row in rows], total=int(total)
        )

    @app.get("/flows/{flow_id}", response_model=TaskView)
    def get_flow(
        flow_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TaskView:
        _, task = load(flow_id, owner_id, session)
        log_event(
            "gateway.flow_read", correlation_id, task_id=flow_id, session_id=owner_id,
            step_id=None, status=task.status,
        )
        return TaskView.model_validate(task)

    @app.get("/flows/{flow_id}/result", response_model=FlowResultView)
    def get_flow_result(
        flow_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> FlowResultView:
        _, task = load(flow_id, owner_id, session)
        result = project_result(task)
        log_event(
            "gateway.flow_result_read",
            correlation_id,
            task_id=flow_id,
            session_id=owner_id,
            step_id=None,
            status=task.status,
            terminal=result.terminal,
            success=result.success,
        )
        return result

    @app.post("/flows/{flow_id}/cancel", response_model=TaskView)
    def cancel_flow(
        flow_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TaskView:
        repository, task = load(flow_id, owner_id, session)
        try:
            cancelled = repository.cancel(task, correlation_id=correlation_id)
        except (InvalidTransition, ConcurrentUpdate) as exc:
            raise HTTPException(409, "cancellation transition unavailable") from exc
        log_event(
            "gateway.flow_cancelled", correlation_id, task_id=flow_id, session_id=owner_id,
            step_id=None, status=cancelled.status,
        )
        return TaskView.model_validate(cancelled)

    @app.post("/flows/{flow_id}/steer", response_model=TaskView)
    def steer_flow(
        flow_id: str,
        body: SteerFlowRequest,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TaskView:
        repository, task = load(flow_id, owner_id, session)
        status_upper = str(task.status).upper()
        if status_upper not in {"WAITING_APPROVAL", "RUNNING"}:
            raise HTTPException(
                400, f"Steering not allowed for flow in status {task.status}"
            )
        try:
            steered = repository.steer(
                task, body.message, actor=owner_id, correlation_id=correlation_id
            )
        except InvalidTransition as exc:
            raise HTTPException(400, str(exc)) from exc
        log_event(
            "gateway.flow_steered",
            correlation_id,
            task_id=flow_id,
            session_id=owner_id,
            step_id=None,
            status=steered.status,
        )
        return TaskView.model_validate(steered)

    @app.get("/flows/{flow_id}/replay/timeline", response_model=TimelineResponse)
    def replay_flow_timeline(
        flow_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> TimelineResponse:
        # Read-only: no repository.transition(), no DONE path, no verifier credential.
        _, task = load(flow_id, owner_id, session)
        from ..replay import ReplayEngine

        engine = ReplayEngine(session)
        events = engine.get_timeline(task.id, owner_id)
        log_event(
            "gateway.flow_replayed", correlation_id, task_id=flow_id, session_id=owner_id,
            step_id=None, status=task.status, view="timeline", entries=len(events),
        )
        return TimelineResponse(
            task_id=task.id,
            entries=[TimelineEntry.model_validate({"type": e.type, "timestamp": e.timestamp, "entity_id": e.entity_id, "description": e.description, "details": e.details}) for e in events],
        )

    @app.get("/flows/{flow_id}/replay", response_model=ReplayResponse)
    def replay_flow(
        flow_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
        actor: str | None = None,
        entity_type: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> ReplayResponse:
        # Read-only: no repository.transition(), no DONE path, no verifier credential.
        _, task = load(flow_id, owner_id, session)
        from ..replay import ReplayEngine

        engine = ReplayEngine(session)
        trajectory = engine.get_trajectory(
            task.id, owner_id, actor=actor, entity_type=entity_type,
            from_dt=from_dt, to_dt=to_dt,
        )
        log_event(
            "gateway.flow_replayed", correlation_id, task_id=flow_id, session_id=owner_id,
            step_id=None, status=task.status, view="trajectory",
        )
        return ReplayResponse.model_validate(trajectory.to_dict())


    @app.get("/approvals", response_model=ApprovalListView)
    def list_approvals(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
        status: str = "PENDING",
        limit: int = 50,
        offset: int = 0,
    ) -> ApprovalListView:
        # Owner scoping runs through the join on task_flows: an approval is
        # only visible to the owner of the flow that raised it. Read-only.
        page = max(1, min(limit, MAX_PAGE_SIZE))
        start = max(0, offset)
        scope = [TaskFlow.owner_id == owner_id]
        if status:
            scope.append(Approval.decision == status)
        joined = select(Approval).join(TaskFlow, Approval.task_id == TaskFlow.id).where(*scope)
        total = session.scalar(
            select(func.count())
            .select_from(Approval)
            .join(TaskFlow, Approval.task_id == TaskFlow.id)
            .where(*scope)
        ) or 0
        rows = session.scalars(
            joined.order_by(Approval.created_at.desc(), Approval.id).limit(page).offset(start)
        ).all()
        log_event(
            "gateway.approvals_listed", correlation_id, task_id=None, session_id=owner_id,
            step_id=None, status=status or "ALL", count=len(rows), total=int(total),
        )
        return ApprovalListView(
            items=[ApprovalListEntry.model_validate(row) for row in rows], total=int(total)
        )

    @app.get("/approvals/{approval_id}", response_model=ApprovalView)
    def get_approval(
        approval_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> ApprovalView:
        approval = session.scalar(select(Approval).where(Approval.id == approval_id))
        if not approval:
            raise HTTPException(404, "approval not found")
        # load() raises 404 (not 403) when the flow belongs to somebody else,
        # so a foreign approval id is indistinguishable from a missing one.
        load(approval.task_id, owner_id, session)
        log_event(
            "gateway.approval_read", correlation_id, task_id=approval.task_id,
            session_id=owner_id, step_id=None, status=approval.decision,
            approval_id=approval_id,
        )
        return ApprovalView.model_validate(approval)

    @app.post("/approvals/{approval_id}/decision", response_model=ApprovalView)
    def decide_approval(
        approval_id: str,
        body: ApprovalDecision,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> ApprovalView:
        pending = session.scalar(select(Approval).where(Approval.id == approval_id))
        if not pending:
            raise HTTPException(404, "approval not found")
        repository, task = load(pending.task_id, owner_id, session)
        try:
            approval = repository.decide_approval(task, approval_id, owner_id, body.approve)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        DurableQueue(session).enqueue(repository.get(task.id), correlation_id=correlation_id)
        log_event(
            "gateway.approval_decided", correlation_id,
            task_id=task.id, session_id=owner_id, step_id=None,
            status="approved" if body.approve else "denied",
            approval_id=approval_id, approve=body.approve,
        )
        return ApprovalView.model_validate(approval)

    @app.websocket("/flows/{flow_id}/progress")
    async def flow_progress(websocket: WebSocket, flow_id: str) -> None:
        cid = ensure_correlation_id(websocket.query_params.get("correlation_id"))
        token = websocket.query_params.get("token", "")
        digest = hashlib.sha256(token.encode()).hexdigest()
        owner_id = next(
            (value for known, value in token_hashes.items() if hmac.compare_digest(digest, known)),
            None,
        )
        if owner_id is None:
            log_event(
                "gateway.websocket_authentication_failed", cid,
                task_id=flow_id, session_id=None, step_id=None, status="4401",
                reason="authentication_denied",
            )
            await websocket.close(code=4401)
            return
        await websocket.accept()
        last_id = 0
        try:
            while True:
                def read(_last: int) -> tuple[list[dict[str, Any]], int, str | None]:
                    events: list[dict[str, Any]] = []
                    max_id = _last
                    with database.session_factory() as session:
                        flow = session.scalar(
                            select(TaskFlow).where(TaskFlow.id == flow_id, TaskFlow.owner_id == owner_id)
                        )
                        if flow is None:
                            return events, max_id, "missing"
                        rows = session.scalars(
                            select(StateTransition)
                            .where(StateTransition.task_id == flow_id, StateTransition.id > _last)
                            .order_by(StateTransition.id)
                        ).all()
                        for row in rows:
                            max_id = max(max_id, row.id)
                            events.append({
                                "type": "transition",
                                "flow_id": flow_id,
                                "entity_id": row.entity_id,
                                "entity_type": row.entity_type,
                                "from_state": row.from_state,
                                "to_state": row.to_state,
                                "reason": row.reason,
                                "actor": row.actor,
                                "correlation_id": row.correlation_id,
                                "created_at": row.created_at.isoformat(),
                            })
                        return events, max_id, flow.status

                events, last_id, status = await asyncio.to_thread(read, last_id)
                if status == "missing":
                    log_event(
                        "gateway.websocket_flow_not_found", cid,
                        task_id=flow_id, session_id=owner_id, step_id=None, status="4404",
                        reason="not_found",
                    )
                    await websocket.send_json({"type": "error", "detail": "flow not found", "correlation_id": cid})
                    await websocket.close(code=4404)
                    return
                for payload in events:
                    await websocket.send_json(payload)
                if status in TERMINAL_STATES and not events:
                    await websocket.send_json({
                        "type": "end", "flow_id": flow_id, "status": status, "correlation_id": cid,
                    })
                    await websocket.close()
                    return
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=0.1)
                    if isinstance(msg, dict) and msg.get("type") == "websocket.disconnect":
                        return
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            return

    @app.get("/events", response_model=list[EventView])
    @app.get("/flows/events", response_model=list[EventView])
    def list_events(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
        after_seq: int = 0,
        limit: int = MAX_PAGE_SIZE,
    ) -> list[EventView]:
        stmt = (
            select(StateTransition)
            .join(TaskFlow, StateTransition.task_id == TaskFlow.id)
            .where(TaskFlow.owner_id == owner_id, StateTransition.id > after_seq)
            .order_by(StateTransition.id)
            .limit(limit)
        )
        rows = session.scalars(stmt).all()
        return [
            EventView(
                type="transition",
                flow_id=row.task_id,
                correlation_id=row.correlation_id,
                from_state=row.from_state,
                to_state=row.to_state,
                reason=row.reason,
                actor=row.actor,
                timestamp=row.created_at.isoformat(),
                seq=row.id,
            )
            for row in rows
        ]

    async def _stream_global_events(websocket: WebSocket) -> None:
        cid = ensure_correlation_id(websocket.query_params.get("correlation_id"))
        token = websocket.query_params.get("token", "")
        raw_after = websocket.query_params.get("after_seq", "0")
        try:
            after_seq = int(raw_after)
        except ValueError:
            after_seq = 0

        digest = hashlib.sha256(token.encode()).hexdigest()
        owner_id = next(
            (value for known, value in token_hashes.items() if hmac.compare_digest(digest, known)),
            None,
        )
        if owner_id is None:
            log_event(
                "gateway.websocket_authentication_failed", cid,
                task_id=None, session_id=None, step_id=None, status="4401",
                reason="authentication_denied",
            )
            await websocket.close(code=4401)
            return

        await websocket.accept()
        last_id = after_seq
        try:
            while True:
                def read(_last: int) -> tuple[list[dict[str, Any]], int]:
                    events: list[dict[str, Any]] = []
                    max_id = _last
                    with database.session_factory() as session:
                        stmt = (
                            select(StateTransition)
                            .join(TaskFlow, StateTransition.task_id == TaskFlow.id)
                            .where(TaskFlow.owner_id == owner_id, StateTransition.id > _last)
                            .order_by(StateTransition.id)
                        )
                        rows = session.scalars(stmt).all()
                        for row in rows:
                            max_id = max(max_id, row.id)
                            events.append({
                                "type": "transition",
                                "flow_id": row.task_id,
                                "correlation_id": row.correlation_id,
                                "from_state": row.from_state,
                                "to_state": row.to_state,
                                "reason": row.reason,
                                "actor": row.actor,
                                "timestamp": row.created_at.isoformat(),
                                "seq": row.id,
                            })
                        return events, max_id

                events, last_id = await asyncio.to_thread(read, last_id)
                for payload in events:
                    await websocket.send_json(payload)
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=0.1)
                    if isinstance(msg, dict) and msg.get("type") == "websocket.disconnect":
                        return
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/events")
    async def global_events_ws(websocket: WebSocket) -> None:
        await _stream_global_events(websocket)

    @app.websocket("/flows/events")
    async def global_flows_events_ws(websocket: WebSocket) -> None:
        await _stream_global_events(websocket)

    # ── SSE Event Stream ─────────────────────────────────────────────

    @app.get("/events/stream")
    async def sse_event_stream(
        request: Request,
        owner_id: Annotated[str, Depends(owner)],
        conversation_id: str = "",
        cursor: str = "",
    ) -> StreamingResponse:
        """Server-Sent Events для Dashboard.

        Step 2 (манифест): события берутся из канонического EventLog
        (таблица ``state_transitions``), а не из легаси JSONL-EventBus.

        Authenticated and owner-scoped, exactly like ``_stream_global_events``:
        the stream must never expose another owner's state transitions.
        """
        from sqlalchemy import select

        from antigona.models import StateTransition

        async def _event_generator() -> AsyncIterator[str]:
            current_seq = 0
            if cursor.isdigit():
                current_seq = int(cursor)
            while True:
                if await request.is_disconnected():
                    return
                with database.session_factory() as session:
                    rows = session.scalars(
                        select(StateTransition)
                        .join(TaskFlow, StateTransition.task_id == TaskFlow.id)
                        .where(
                            TaskFlow.owner_id == owner_id,
                            StateTransition.id > current_seq,
                        )
                        .order_by(StateTransition.id.asc())
                        .limit(50)
                    )
                    transitions = list(rows)
                for row in transitions:
                    current_seq = row.id
                    event = {
                        "event_id": str(row.id),
                        "event_type": "flow.state_changed",
                        "flow_id": row.task_id,
                        "from_state": row.from_state,
                        "to_state": row.to_state,
                        "actor": row.actor,
                        "reason": row.reason[:500],
                        "correlation_id": row.correlation_id,
                        "created_at": row.created_at.isoformat(),
                    }
                    if conversation_id and conversation_id not in (
                        row.task_id,
                        row.correlation_id,
                    ):
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/skills")
    def list_skills(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> list[dict[str, Any]]:
        registry = SkillsRegistry(session)
        skills = registry.list_skills()
        return [
            {
                "id": s.id,
                "name": s.name,
                "slug": s.slug,
                "version": s.version,
                "status": s.status,
                "owner_id": s.owner_id,
                "trust": s.trust,
                "risk_ceiling": s.risk_ceiling,
                "created_at": s.created_at.isoformat(),
            }
            for s in skills
            if s.owner_id == owner_id or s.status == "ACTIVE"
        ]

    @app.get("/skills/{skill_id}")
    def get_skill_detail(
        skill_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        registry = SkillsRegistry(session)
        skill = registry.get_skill(skill_id)
        if not skill:
            raise HTTPException(404, f"skill {skill_id} not found")

        state_root = os.environ.get("ANTIGONA_STATE_ROOT", "/var/lib/antigona")
        body_text = ""
        if skill.body_sha256:
            try:
                body_bytes = registry.get_card_body(skill, state_root)
                body_text = body_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass

        return {
            "id": skill.id,
            "name": skill.name,
            "slug": skill.slug,
            "version": skill.version,
            "status": skill.status,
            "owner_id": skill.owner_id,
            "trust": skill.trust,
            "risk_ceiling": skill.risk_ceiling,
            "revision": skill.revision,
            "body_sha256": skill.body_sha256,
            "body_bytes": skill.body_bytes,
            "body": body_text,
            "created_at": skill.created_at.isoformat(),
        }

    @app.post("/skills/capture")
    def capture_skill(
        body: dict[str, Any],
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        """Capture-навыка из завершённого флоу (Step 3: только ядро пишет БД)."""
        from ..skills.capture import capture_from_flow

        flow_id = str(body.get("flow_id") or "").strip()
        if not flow_id:
            raise HTTPException(422, "flow_id is required")
        state_root = str(body.get("state_root") or os.environ.get(
            "ANTIGONA_STATE_ROOT", "/var/lib/antigona"
        ))
        registry = SkillsRegistry(session)
        try:
            skill_row = capture_from_flow(
                session,
                flow_id,
                owner_id=owner_id,
                state_root=state_root,
                registry=registry,
            )
        except Exception as exc:
            logger = __import__("logging").getLogger("antigona.gateway")
            logger.warning("skill capture failed for flow %s: %s", flow_id, exc)
            raise HTTPException(422, f"capture failed: {exc}") from exc
        return {
            "id": skill_row.id,
            "slug": getattr(skill_row, "slug", ""),
            "status": getattr(skill_row, "status", "DRAFT"),
        }

    @app.post("/skills/{skill_id}/deprecate")
    def deprecate_skill(
        skill_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        """Deprecate-навыка (Step 3: только ядро пишет БД)."""
        registry = SkillsRegistry(session)
        try:
            deprecated = registry.deprecate(
                skill_id,
                actor=f"cli:{owner_id}",
                correlation_id=correlation_id,
            )
        except Exception as exc:
            raise HTTPException(422, f"deprecate failed: {exc}") from exc
        return {
            "id": getattr(deprecated, "id", skill_id),
            "status": getattr(deprecated, "status", "DEPRECATED"),
        }

    @app.post("/skills/{skill_id}/promote")
    def proxy_promote_skill(
        skill_id: str,
        body: dict[str, Any],
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        import httpx

        from ..security import verifier_credential

        verifier_url = os.environ.get(
            "ANTIGONA_VERIFIER_URL", "http://127.0.0.1:8091"
        )
        secret = verifier_credential()

        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            CORRELATION_HEADER: correlation_id,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(
                    f"{verifier_url}/skills/{skill_id}/promote",
                    json=body,
                    headers=headers,
                )
            if res.status_code != 200:
                raise HTTPException(res.status_code, res.text)
            return cast(dict[str, Any], res.json())
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"verifier service unavailable: {exc}") from exc

    # ── Cron schedule endpoints ──────────────────────────────────────

    @app.post("/schedules", response_model=ScheduleView, status_code=201)
    def create_schedule(
        body: ScheduleCreate,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> ScheduleView:
        from ..cron import CronScheduler

        scheduler = CronScheduler(session)
        try:
            sched = scheduler.create_schedule(
                name=body.name,
                cron_expression=body.cron_expression,
                owner_id=owner_id,
                goal=body.goal,
                target_path=body.target_path,
                content=body.content,
                tool_name=body.tool_name,
                tool_arguments=body.tool_arguments,
                correlation_id=correlation_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log_event(
            "gateway.schedule_created",
            correlation_id,
            task_id=sched.id,
            session_id=owner_id,
            step_id=None,
            status="created",
        )
        return ScheduleView.model_validate(sched)

    @app.get("/schedules", response_model=list[ScheduleView])
    def list_schedules(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> list[ScheduleView]:
        from ..cron import CronScheduler

        scheduler = CronScheduler(session)
        schedules = scheduler.list_schedules(owner_id=owner_id)
        return [ScheduleView.model_validate(s) for s in schedules]

    @app.get("/schedules/{schedule_id}", response_model=ScheduleView)
    def get_schedule(
        schedule_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> ScheduleView:
        from ..cron import CronScheduleNotFound, CronScheduler

        scheduler = CronScheduler(session)
        try:
            sched = scheduler.get_schedule(schedule_id, owner_id=owner_id)
        except CronScheduleNotFound as exc:
            raise HTTPException(404, "schedule not found") from exc
        return ScheduleView.model_validate(sched)

    @app.get("/schedules/{schedule_id}/jobs")
    def get_schedule_jobs(
        schedule_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> list[dict[str, Any]]:
        from ..cron import CronScheduleNotFound, CronScheduler

        scheduler = CronScheduler(session)
        try:
            flows = scheduler.get_jobs(schedule_id, owner_id=owner_id)
        except CronScheduleNotFound as exc:
            raise HTTPException(404, "schedule not found") from exc
        log_event(
            "gateway.schedule_jobs_listed",
            correlation_id,
            task_id=schedule_id,
            session_id=owner_id,
            step_id=None,
            status="ok",
            count=len(flows),
        )
        return [
            {"id": f.id, "goal": f.goal, "status": f.status, "created_at": f.created_at.isoformat()}
            for f in flows
        ]

    @app.post("/schedules/{schedule_id}/cancel", response_model=ScheduleView)
    def cancel_schedule(
        schedule_id: str,
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> ScheduleView:
        from ..cron import CronScheduleNotFound, CronScheduler

        scheduler = CronScheduler(session)
        try:
            sched = scheduler.cancel_schedule(
                schedule_id, owner_id=owner_id, correlation_id=correlation_id
            )
        except CronScheduleNotFound as exc:
            raise HTTPException(404, "schedule not found") from exc
        log_event(
            "gateway.schedule_cancelled",
            correlation_id,
            task_id=schedule_id,
            session_id=owner_id,
            step_id=None,
            status="cancelled",
        )
        return ScheduleView.model_validate(sched)

    @app.post("/schedules/tick")
    def trigger_tick(
        owner_id: Annotated[str, Depends(owner)],
        session: Annotated[Session, Depends(get_session)],
        correlation_id: Annotated[str, Depends(correlation)],
    ) -> dict[str, Any]:
        from ..cron import CronScheduler

        scheduler = CronScheduler(session)
        tasks = scheduler.tick(correlation_id=correlation_id)
        return {
            "ticked": True,
            "tasks_created": len(tasks),
            "errors": scheduler.last_tick_error_count,
        }

    # ── M2 Goal Orchestration routes ────────────────────────────────────────
    from antigona.kernel import KernelStore
    from antigona.orchestration import OrchestrationStore
    from antigona.orchestration.autonomy import AutonomyContractError

    @app.post("/goals", status_code=201)
    def create_goal(
        body: dict[str, Any],
        owner_id: Annotated[str, Depends(owner)],
    ) -> dict[str, Any]:
        orch = OrchestrationStore(database.session_factory)
        objective = str(body.get("objective") or "").strip()
        if not objective:
            raise HTTPException(422, "objective is required")
        workspace = str(body.get("workspace") or "").strip()
        if not workspace:
            raise HTTPException(422, "workspace is required")
        test_command = body.get("test_command")
        if test_command is not None and (
            not isinstance(test_command, list)
            or not test_command
            or not all(isinstance(item, str) and item for item in test_command)
        ):
            raise HTTPException(422, "test_command must be a non-empty argv list")
        mutation_required = body.get("mutation_required", False)
        if not isinstance(mutation_required, bool):
            raise HTTPException(422, "mutation_required must be a boolean")
        try:
            goal = orch.create_goal(
                objective=objective,
                owner_id=owner_id,
                session_id=str(body.get("session_id") or ""),
                acceptance_criteria=body.get("acceptance_criteria"),
                max_cycles=int(body.get("max_cycles") or 3),
                budget=body.get("budget"),
                workspace=workspace,
                mutation_required=mutation_required,
                test_command=test_command,
            )
        except AutonomyContractError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"goal_id": goal.id, "status": goal.status, "objective": goal.objective[:200]}

    @app.get("/goals")
    def list_goals(
        owner_id: Annotated[str, Depends(owner)],
        status: str | None = None,
    ) -> dict[str, Any]:
        orch = OrchestrationStore(database.session_factory)
        goals = orch.list_goals(status=status, owner_id=owner_id, limit=50)
        return {
            "goals": [
                {
                    "goal_id": g.id,
                    "status": g.status,
                    "objective": g.objective[:200],
                    "cycle_count": g.cycle_count,
                    "max_cycles": g.max_cycles,
                    "created_at": g.created_at.isoformat() if g.created_at else None,
                }
                for g in goals
            ]
        }

    @app.get("/goals/{goal_id}")
    def goal_detail(
        goal_id: str,
        owner_id: Annotated[str, Depends(owner)],
    ) -> dict[str, Any]:
        orch = OrchestrationStore(database.session_factory)
        kernel = KernelStore(database.session_factory)
        goal = orch.get_goal(goal_id)
        if goal is None or goal.owner_id != owner_id:
            # IDOR guard: other owners' goals are indistinguishable from
            # missing (Grok audit Security MAJOR).
            raise HTTPException(404, "goal not found")
        flow = orch.get_current_flow(goal_id)
        tasks: list[dict[str, Any]] = []
        if flow and flow.plan.get("task_ids"):
            for tid in flow.plan["task_ids"]:
                t = kernel.get_task(str(tid))
                if t is not None:
                    tasks.append(
                        {
                            "task_id": t.id,
                            "status": t.status,
                            "stage": (t.payload or {}).get("stage", ""),
                        }
                    )
        handoffs = orch.list_handoffs(goal_id=goal_id, limit=20)
        return {
            "goal_id": goal.id,
            "status": goal.status,
            "objective": goal.objective,
            "acceptance_criteria": goal.acceptance_criteria,
            "cycle_count": goal.cycle_count,
            "max_cycles": goal.max_cycles,
            "result": goal.result,
            "failure_reason": goal.failure_reason,
            "current_flow_id": goal.current_flow_id,
            "flow_status": flow.status if flow else None,
            "flow_revision": flow.revision if flow else None,
            "flow_stage": flow.current_stage if flow else None,
            "tasks": tasks,
            "handoff_count": len(handoffs),
        }

    @app.get("/orchestration/status")
    def orchestration_status(
        owner_id: Annotated[str, Depends(owner)],
    ) -> dict[str, Any]:
        orch = OrchestrationStore(database.session_factory)
        active = orch.list_active_goals(limit=10)
        return {
            "health": orch.health_snapshot(),
            "active_goals": len(active),
            "goals": [{"goal_id": g.id, "status": g.status} for g in active],
        }

    return app
