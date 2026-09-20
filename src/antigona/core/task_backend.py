"""GatewayTaskBackend — серверный execution-контракт для AntigonaBrain.

Реализует :class:`antigona.core.brain.TaskBackend` поверх единого
:class:`antigona.core.task_service.TaskSubmissionService` — того же сервиса,
который используют HTTP-эндпоинты ``POST /flows`` и ``POST /tasks``. Никакого
HTTP и никакой второй независимой реализации task submission.

Используется ТОЛЬКО сервером (gateway/api.py). CLI и Telegram никогда не создают
этот backend — они ходят в Gateway через единый /api/v1/dialogue/turn.
"""

from __future__ import annotations

import logging
from typing import Any

from antigona.core.task_service import TaskSubmissionService
from antigona.database import Database
from antigona.repository import TaskRepository
from antigona.task_goal import resolve_free_text_request

logger = logging.getLogger(__name__)


class GatewayTaskBackend:
    """Внутренний бэкенд задач в каноническом Gateway.

    Args:
        database: Единый Database (session_factory) серверного runtime.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._service = TaskSubmissionService(database)

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        tool_name: str | None = None,
        command: tuple[str, ...] = (),
        path: str | None = None,
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
    ) -> dict[str, Any]:
        """Создать задачу через единый TaskSubmissionService.

        tool_name/command/path/content: явное указание инструмента
        (например ``sandbox.shell`` + command для shell-запросов).
        Когда tool_name не задан, запрос — свободный текст, и инструмент
        определяется КАНОНИЧЕСКИМ резолвером цели
        (:func:`antigona.task_goal.resolve_free_text_request`) — тем же,
        что использует ``POST /tasks``. Жёсткого дефолта
        ``workspace.write_text`` здесь больше нет: он превращал любой
        свободный запрос (в том числе shell-цель) в запись собственного
        текста, из-за чего verifier отвечал владельцу отказом вместо
        выполнения команды (FP-L05b).
        mcp_server/mcp_tool/mcp_arguments: параметры MCP-вызова для
        tool_name="mcp". params: общие параметры инструмента
        (например send_email: to/subject/body/attachment).
        """
        if tool_name is None:
            request = resolve_free_text_request(message)
            tool_name = request.tool_name
            if path is None:
                path = request.path or None
            if content is None:
                content = request.content
            if not command:
                command = request.command
            if request.answer_only:
                params = {**(params or {}), "answer_only": True}

        return await self._service.submit_async(
            owner_id=owner_id or "default",
            message=message,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            client=client,
            metadata=metadata,
            tool_name=tool_name,
            command=command,
            path=path,
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
        )

    async def cancel_flow(self, flow_id: str) -> Any:
        """Отменить активную задачу."""
        with self.database.session_factory() as session:
            repository = TaskRepository(session)
            try:
                task = repository.get(flow_id)
            except Exception as exc:
                raise RuntimeError(f"flow not found: {flow_id}") from exc
            return repository.cancel(task, correlation_id=None)

    async def get_flow(self, flow_id: str) -> Any:
        """Получить состояние задачи."""
        with self.database.session_factory() as session:
            try:
                return TaskRepository(session).get(flow_id)
            except Exception as exc:
                raise RuntimeError(f"flow not found: {flow_id}") from exc

    async def steer_flow(self, flow_id: str, message: str) -> Any:
        """Скорректировать выполняющуюся задачу."""
        with self.database.session_factory() as session:
            repository = TaskRepository(session)
            try:
                task = repository.get(flow_id)
            except Exception as exc:
                raise RuntimeError(f"flow not found: {flow_id}") from exc
            return repository.steer(task, message, actor="core", correlation_id=None)
