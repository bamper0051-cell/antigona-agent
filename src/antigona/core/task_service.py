"""TaskSubmissionService — единый серверный сервис создания задач.

Каноническая логика submit (policy checks, owner binding, idempotency,
verifier criteria, durable queue, audit-лог) вынесена из HTTP-эндпоинтов
в один application service. И ``POST /flows``, и ``POST /tasks``, и
``AntigonaBrain`` (через :class:`GatewayTaskBackend`) вызывают ОДИН сервис —
никакой второй независимой реализации task submission.

Владелец запроса (owner_id) и correlation_id передаются явными аргументами,
никогда не хранятся на общем singleton (защита от cross-request races).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from antigona.database import Database
from antigona.queue import DurableQueue
from antigona.repository import (
    CreateTask,
    IdempotencyConflict,
    SensitiveTaskInput,
    TaskRepository,
)
from antigona.task_goal import (
    ANSWER_ONLY_TOOL,
    expected_paths_from_goal,
    requires_exact_write_read_contract,
    resolve_free_text_request,
)

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "task_output.txt"


class TaskSubmissionService:
    """Application service: создание TaskFlow через канонический submit.

    Args:
        database: Единый Database серверного runtime.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def submit(
        self,
        *,
        owner_id: str,
        message: str,
        idempotency_key: str | None = None,
        tool_name: str = "workspace.write_text",
        path: str | None = None,
        command: tuple[str, ...] = (),
        content: str | None = None,
        read_after_write: bool = False,
        run_after_write: bool = False,
        run_command: tuple[str, ...] = (),
        fix_after_run: bool = False,
        fix_content: str = "",
        fix_command: tuple[str, ...] = (),
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Создать задачу через канонический путь и вернуть TaskView-подобный dict.

        Сохраняет:
            - policy checks (SensitiveTaskInput → SensitiveTaskInput);
            - owner binding (CreateTask.owner_id);
            - idempotency (IdempotencyConflict → IdempotencyConflict);
            - verifier criteria (Goal satisfied);
            - durable queue enqueue;
            - audit (log_event gateway.flow_created).

        Args:
            content: Отдельный content для CreateTask (для /flows: body.content;
                для /tasks и brain: == message).
            mcp_server/mcp_tool/mcp_arguments: Параметры MCP-вызова для
                tool_name="mcp" (оркестратор подключается к зарегистрированному
                серверу и вызывает тулу).
            params: Общие параметры инструмента для tool_name с params
                (например send_email: to/subject/body/attachment).

        Returns:
            dict с flow_id / id / status / requires_approval / created.
        """
        from antigona.config import Settings
        from antigona.gateway.correlation import log_event
        from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore

        idem = idempotency_key or correlation_id or f"task-{uuid.uuid4().hex[:12]}"
        cid = correlation_id or uuid.uuid4().hex

        # Shell tasks without an explicit target path are stdout-only turns
        # (e.g. "Выполни echo X" from the dialogue): the orchestrator must
        # not demand a written file — it should materialize the stdout.
        # MCP tasks never write a file: the tool result text is the artifact.
        # Email tasks have no file target either.
        resolved_path = path
        if tool_name == "sandbox.shell" and command and path is None:
            resolved_path = "stdout"
        if tool_name in ("mcp", "send_email") and path is None:
            resolved_path = "mcp-result" if tool_name == "mcp" else "email"

        if tool_name == "workspace.write_text":
            expected_paths = expected_paths_from_goal(message)
            drafted_paths = expected_paths_from_goal(content or "")
            # A path-less explicit draft is a distinct submission contract: its
            # explicit target may intentionally differ from a filename mentioned
            # in the conversational request.  In degraded mode the message is the
            # inferred body, and a drafted body that names a path still binds the
            # target fail-closed so substituted targets cannot be accepted.
            # A path-less explicit draft is a distinct submission contract: its
            # explicit target may intentionally differ from a filename mentioned
            # in the conversational request (LOOP-3). The goal path binds only in
            # degraded mode (content=None, message is the inferred body) or when
            # the draft itself names a path. `bool(expected_paths)` alone must
            # not force a match, or an explicit path-less draft writing to a
            # different target is wrongly rejected as a substitution.
            goal_path_is_binding = content is None or bool(drafted_paths)
            if (
                goal_path_is_binding
                and expected_paths
                and resolved_path
                and resolved_path not in expected_paths
            ):
                raise SensitiveTaskInput(
                    "goal path does not match target path: "
                    f"expected one of {expected_paths!r}, got {resolved_path!r}"
                )

        # LOOP3 / DEFECT 3: ``content is None`` означает «у вызывающей стороны
        # нет черновика содержимого». FP-L05d: degraded-режим больше НЕ подставляет
        # текст запроса как тело файла (живой дефект 4857f8d1 и машина ложного
        # DONE). Тело РЕЗОЛВИТСЯ из самой цели каноническим резолвером; если тело
        # из запроса не выводится — задача уходит в answer_only (fail-closed), а
        # не записывает инструкцию в файл.
        resolved_content = content
        if resolved_content is None:
            if tool_name == "workspace.write_text":
                request = resolve_free_text_request(message)
                if request.tool_name == "workspace.write_text" and not request.answer_only:
                    resolved_content = request.content or ""
                    logger.warning(
                        "task submit without drafted content: using the body named by "
                        "the request itself (degraded mode, path=%s)",
                        resolved_path or _DEFAULT_PATH,
                    )
                else:
                    params = {**(params or {}), "answer_only": True}
                    tool_name = ANSWER_ONLY_TOOL
                    resolved_content = ""
                    logger.warning(
                        "task submit without drafted content and no derivable body: "
                        "submitted as answer-only (degraded mode, path=%s, reason=%s)",
                        resolved_path or _DEFAULT_PATH,
                        request.reason,
                    )
            else:
                # Non-write tools (shell/read/mcp/email) have no file body at all:
                # the request text must never become one.
                resolved_content = ""

        effective_read_after_write = read_after_write or requires_exact_write_read_contract(
            message, content=resolved_content, tool_name=tool_name
        )

        with self.database.session_factory() as session:
            repository = TaskRepository(session)
            try:
                task, created = repository.create(
                    CreateTask(
                        owner_id=owner_id or "default",
                        goal=message,
                        path=resolved_path or _DEFAULT_PATH,
                        content=resolved_content,
                        idempotency_key=idem,
                        tool_name=tool_name,
                        command=command,
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        mcp_arguments=mcp_arguments or {},
                        params=params or {},
                        read_after_write=effective_read_after_write,
                        run_after_write=bool(run_after_write and run_command),
                        run_command=tuple(run_command),
                        fix_after_run=bool(fix_after_run),
                        fix_content=fix_content,
                        fix_command=tuple(fix_command),
                    ),
                    correlation_id=cid,
                )
            except SensitiveTaskInput as exc:
                raise SensitiveTaskInput(str(exc)) from exc
            except IdempotencyConflict as exc:
                raise IdempotencyConflict(str(exc)) from exc

            if created:
                from antigona.models import StepState, TaskState

                criteria_db: VerifierCriteriaDatabase | None = None
                criteria_ok = False
                try:
                    criteria_db = VerifierCriteriaDatabase(Settings.from_env().database_url)
                    # The task submitter may be the first process touching a fresh
                    # database. The verifier-private schema must therefore be
                    # initialized here too; create_all() is idempotent.
                    criteria_db.create_all()
                    with criteria_db.session_factory() as criteria_session:
                        # BUG ANT-002 P2: read-tasks got the generic "Goal satisfied"
                        # criteria, so the LLM judge had no way to know the artifact
                        # (the file's content) IS the delivered result and rejected
                        # correct reads as "irrelevant". Give read-tasks explicit
                        # criteria so the judge checks non-empty content only.
                        if tool_name == "workspace.read_text":
                            _criteria = (
                                "1. Output matches the actual content of the read file\n"
                                "2. Output is not empty\n"
                                "3. No errors occurred during reading"
                            )
                        else:
                            _criteria = f"Goal satisfied: {message}"
                        VerifierCriteriaStore(criteria_session).put(task.id, _criteria)
                        criteria_session.commit()
                    criteria_ok = True
                except Exception as exc:
                    logger.error(
                        "verifier criteria write failed for %s; task cancelled "
                        "without enqueue (error_type=%s)",
                        task.id,
                        type(exc).__name__,
                    )
                finally:
                    if criteria_db is not None:
                        criteria_db.engine.dispose()

                if criteria_ok:
                    DurableQueue(session).enqueue(task, correlation_id=cid)
                else:
                    for step in task.steps:
                        if step.status in {StepState.PENDING.value, StepState.RUNNING.value}:
                            repository.transition_step(
                                task,
                                step,
                                StepState.CANCELLED,
                                reason="verifier criteria unavailable; task not queued",
                                actor="task-service",
                                correlation_id=cid,
                            )
                    repository.transition(
                        task,
                        TaskState.CANCELLED,
                        reason="verifier criteria unavailable; task not queued",
                        actor="task-service",
                        correlation_id=cid,
                    )
                    session.commit()

            refreshed = TaskRepository(session).get(task.id)
            log_event(
                "gateway.flow_created",
                cid,
                task_id=task.id,
                session_id=conversation_id or owner_id,
                step_id=None,
                status=refreshed.status,
                created=created,
                client=client,
            )
            return {
                "flow_id": refreshed.id,
                "id": refreshed.id,
                "status": refreshed.status,
                "requires_approval": False,
                "created": created,
            }

    async def submit_async(
        self,
        *,
        owner_id: str,
        message: str,
        idempotency_key: str | None = None,
        tool_name: str = "workspace.write_text",
        path: str | None = None,
        command: tuple[str, ...] = (),
        content: str | None = None,
        read_after_write: bool = False,
        run_after_write: bool = False,
        run_command: tuple[str, ...] = (),
        fix_after_run: bool = False,
        fix_content: str = "",
        fix_command: tuple[str, ...] = (),
        mcp_server: str = "",
        mcp_tool: str = "",
        mcp_arguments: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Async-обёртка над :meth:`submit` (sync SQLAlchemy внутри)."""
        return self.submit(
            owner_id=owner_id,
            message=message,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            path=path,
            command=command,
            content=content,
            read_after_write=read_after_write,
            run_after_write=run_after_write,
            run_command=run_command,
            fix_after_run=fix_after_run,
            fix_content=fix_content,
            fix_command=fix_command,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            mcp_arguments=mcp_arguments,
            params=params,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            client=client,
            metadata=metadata,
        )
