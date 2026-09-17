"""GatewayClient — HTTP-клиент для Gateway (реализация AntigonaControlPlane).

Вызовы идут на http://127.0.0.1:8090 (локальный Gateway).
Используется Telegram, CLI и Dashboard как единственный способ управления задачами.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

import httpx

from antigona.core.control_plane import (
    ApprovalListEntry,
    ApprovalListView,
    ApprovalView,
    FlowStatus,
    FlowView,
    NormalizedRequest,
    SteeringCommand,
)

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://127.0.0.1:8090"

TERMINAL_FLOW_STATUSES = frozenset(
    {
        FlowStatus.DONE,
        FlowStatus.FAILED,
        FlowStatus.BLOCKED,
        FlowStatus.CANCELLED,
        FlowStatus.TIMEOUT,
        FlowStatus.POLICY_DENIED,
    }
)

# Budget floor for the *mandatory* authoritative reads (the final flow GET on a
# deadline/cancellation, and the ``/result`` fetch that validates a terminal
# observation).  Those reads must still be issued when the overall wait budget
# is already exhausted, so a small floor is the only way the waiter can honour
# "one authoritative read" without hanging on I/O.  It also caps by how much a
# slow Gateway can push the waiter past the caller's deadline.
DEFAULT_FINAL_READ_GRACE = 0.5

# How many artifacts a synthesized success summary may name.
_ARTIFACT_SUMMARY_LIMIT = 3


@dataclass
class GatewayFlowView(FlowView):
    """``FlowView`` plus the raw authoritative ``revision`` from the Gateway.

    ``control_plane.FlowView`` is the transport-agnostic projection shared by
    every interface and deliberately has no revision field, so the lossy
    projection used to drop it — and with it any chance to check that a
    ``/result`` body describes the very same flow revision that was observed as
    terminal.  Subclassing (instead of widening the shared contract) keeps all
    existing call sites, ``isinstance`` checks and ``dataclasses.replace``
    usage working while preserving the authoritative value.
    """

    revision: int | None = None


@dataclass(frozen=True, slots=True)
class GatewayResultArtifact:
    """Immutable client projection of a verifier-confirmed artifact."""

    path: str
    sha256: str
    size: int
    verified: bool


@dataclass(frozen=True, slots=True)
class GatewayFlowResult:
    """Immutable client DTO matching the Gateway ``FlowResultView`` schema."""

    flow_id: str
    status: FlowStatus
    terminal: bool
    success: bool
    artifacts: tuple[GatewayResultArtifact, ...]
    safe_result_text: str | None
    stdout_preview: str | None
    failure_reason: str | None
    completed_at: str | None
    # ``None`` means the payload carried no revision at all (older/mocked
    # bodies).  It must stay distinguishable from a real ``0``: only two
    # *present* revisions may be compared, and a conflict is rejected.
    revision: int | None = None


# Short aliases keep the public client API readable without coupling it to the
# server-side Pydantic schema module.
FlowResult = GatewayFlowResult
VerifiedResultArtifact = GatewayResultArtifact


class GatewayError(RuntimeError):
    """Базовое исключение для ошибок Gateway."""


class GatewayHTTPError(GatewayError):
    """HTTP-ошибка от Gateway (4xx/5xx)."""


class GatewayFlowNotFoundError(GatewayHTTPError):
    """Flow не найден в Gateway (HTTP 404)."""

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id
        super().__init__(f"Flow not found: {flow_id}")


class GatewayConnectionError(GatewayError):
    """Gateway недоступен."""


class GatewayTimeoutError(GatewayError):
    """Таймаут при обращении к Gateway."""


class GatewayValidationError(GatewayError):
    """Payload не пройдёт схему Gateway — не отправляем запрос вообще."""


class GatewayProtocolError(GatewayValidationError):
    """A Gateway *response* violated the client-side terminal-result contract.

    Subclasses ``GatewayValidationError`` so existing handlers keep working,
    while giving callers a way to tell "we refused to send" apart from "the
    server answered something we must not trust".  Messages are fixed strings:
    no raw bodies, URLs or echoed values ever reach the caller.
    """


class GatewayWaitTimeoutError(GatewayTimeoutError):
    """The caller's overall terminal-wait deadline elapsed."""

    def __init__(self, flow_id: str, timeout: float) -> None:
        self.flow_id = flow_id
        self.timeout = float(timeout)
        self.timeout_seconds = self.timeout
        super().__init__(
            f"Timed out waiting {self.timeout:g}s for flow {flow_id} to become terminal"
        )


class GatewayWaitCancelledError(GatewayError):
    """The local caller cancelled waiting without cancelling the remote flow."""

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id
        super().__init__(f"Waiting for flow {flow_id} was cancelled locally")


class GatewayClient:
    """HTTP-клиент к Gateway. Реализует AntigonaControlPlane."""

    def __init__(
        self,
        base_url: str = GATEWAY_URL,
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._base_url = self.base_url
        self._token = self.token
        self.transport = transport
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=30, headers=headers, transport=transport, trust_env=False
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GatewayClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


    async def submit(self, request: NormalizedRequest) -> FlowView:
        """Создать задачу на Gateway.

        Gateway /flows POST ожидает TaskCreate (goal, path, content, tool_name, command)
        + заголовки Idempotency-Key и X-Correlation-Id.
        """
        idempotency_key = str(uuid.uuid4())
        correlation_id = request.correlation_id or str(uuid.uuid4())

        # Pre-flight validation: Gateway's TaskCreate schema requires
        # goal/path with min_length=1. Failing fast here avoids a wasted
        # round-trip that would come back as a 422 the caller has to
        # decode from an HTTP error body.
        goal_text = (request.user_message or "").strip()
        if not goal_text:
            logger.error(
                "Gateway.submit: отказ — пустой user_message "
                "(source=%s, correlation_id=%s); /flows отклонит goal с "
                "min_length=1",
                request.source,
                correlation_id[:12],
            )
            raise GatewayValidationError(
                "Не удалось создать задачу: пустой текст запроса."
            )

        # Embedded Telegram actions carry structured metadata.  Preserve it at
        # the Gateway boundary instead of submitting an unrelated write of the
        # literal action command to ``/``.
        metadata = request.metadata or {}
        action_type = str(metadata.get("action_type") or "").upper()
        if not action_type:
            # Generic free-text submission via POST /tasks — same endpoint
            # the CLI chat uses.  No structured action metadata required.
            try:
                result = await self.submit_task(
                    message=goal_text,
                    conversation_id=request.conversation_id,
                    client=request.source,
                    metadata={
                        "correlation_id": correlation_id,
                        "source": request.source,
                    },
                    idempotency_key=idempotency_key,
                )
                return self._parse_flow(result)
            except httpx.HTTPStatusError as exc:
                detail: Any = _safe_error_detail(exc.response.text[:500])
                logger.error(
                    "Gateway POST /tasks вернул %d: %s",
                    exc.response.status_code,
                    detail,
                )
                raise GatewayHTTPError(
                    f"Gateway ответил ошибкой {exc.response.status_code}: {detail}"
                ) from exc
            except httpx.ConnectError as exc:
                raise GatewayConnectionError(
                    "Gateway недоступен, попробуйте позже"
                ) from exc
            except httpx.TimeoutException as exc:
                raise GatewayTimeoutError(
                    "Время ожидания Gateway истекло"
                ) from exc

        payload: dict[str, Any] = {"goal": goal_text}
        if action_type == "WRITE_FILE":
            path = str(metadata.get("path") or "").strip()
            if not path:
                raise GatewayValidationError("WRITE_FILE requires a non-empty path")
            payload.update(
                path=path,
                content=str(metadata.get("content") or ""),
                tool_name="workspace.write_text",
                command=[],
            )
        elif action_type in {"RUN_SHELL", "RUN_CODE"}:
            raw_command = metadata.get("command")
            if action_type == "RUN_CODE":
                language = str(metadata.get("path") or "python").lower().strip()
                code = str(metadata.get("content") or "")
                if not code:
                    raise GatewayValidationError("RUN_CODE requires non-empty code")
                if language in {"python", "py"}:
                    command = ["python", "-c", code]
                elif language in {"bash", "sh"}:
                    command = ["bash", "-lc", code]
                else:
                    raise GatewayValidationError(
                        f"Unsupported RUN_CODE language: {language}"
                    )
            elif isinstance(raw_command, list):
                command = [str(part) for part in raw_command]
            else:
                try:
                    command = shlex.split(str(raw_command or ""))
                except ValueError as exc:
                    raise GatewayValidationError(
                        f"Invalid RUN_SHELL command: {exc}"
                    ) from exc
            if not command:
                raise GatewayValidationError(f"{action_type} requires a command")
            payload.update(
                path=str(metadata.get("path") or "workspace"),
                content=str(metadata.get("content") or ""),
                tool_name="sandbox.shell",
                command=command,
            )
        else:
            raise GatewayProtocolError(
                "Unsupported action type for the current Gateway API"
            )

        try:
            resp = await self._client.post(
                "/flows",
                json=payload,
                headers={
                    "Idempotency-Key": idempotency_key,
                    "X-Correlation-Id": correlation_id,
                },
            )
        except httpx.ConnectError as exc:
            logger.error(
                "Gateway недоступен (%s): payload=%s",
                exc,
                _truncate_json(payload),
            )
            raise GatewayConnectionError(
                "Gateway недоступен, попробуйте позже"
            ) from exc
        except httpx.TimeoutException as exc:
            logger.error("Gateway timeout: payload=%s", _truncate_json(payload))
            raise GatewayTimeoutError(
                "Время ожидания Gateway истекло"
            ) from exc
        except httpx.HTTPError as exc:
            logger.error(
                "HTTP error calling Gateway: %s — payload=%s",
                exc,
                _truncate_json(payload),
            )
            raise GatewayConnectionError(
                f"Ошибка соединения с Gateway: {exc}"
            ) from exc

        if resp.is_error:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]

            logger.error(
                "Gateway вернул %d: detail=%s — payload=%s",
                resp.status_code,
                _safe_error_detail(detail),
                _truncate_json(payload),
            )
            raise GatewayHTTPError(
                f"Gateway ответил ошибкой {resp.status_code}"
            )

        data = resp.json()
        return self._parse_flow(data)

    async def steer_flow(self, flow_id: str, message: str) -> dict[str, Any]:
        """Отправить steering-сообщение в поток flow_id через POST /flows/{flow_id}/steer.

        Проверяет существование задачи и её статус (WAITING_APPROVAL / RUNNING).
        При неверном статусе (400) или ненайденном flow (404) выбрасывает GatewayHTTPError.
        """
        payload = {"message": message}
        try:
            resp = await self._client.post(f"/flows/{flow_id}/steer", json=payload)
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            return res
        except httpx.HTTPStatusError as exc:
            detail: Any = _safe_error_detail(exc.response.text[:500])
            logger.error(
                "Gateway POST /flows/%s/steer вернул %d: %s",
                flow_id,
                exc.response.status_code,
                detail,
            )
            raise GatewayHTTPError(
                f"Gateway ответил ошибкой {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.ConnectError as exc:
            raise GatewayConnectionError(
                "Gateway недоступен, попробуйте позже"
            ) from exc
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                "Время ожидания Gateway истекло"
            ) from exc

    async def steer(self, flow_id: str, command: SteeringCommand) -> FlowView:
        msg = command.modification_text or command.command
        res = await self.steer_flow(flow_id, msg)
        return self._parse_flow(res)


    async def cancel(self, flow_id: str, reason: str = "") -> FlowView:
        resp = await self._client.post(
            f"/flows/{flow_id}/cancel", json={"reason": reason}
        )
        resp.raise_for_status()
        return self._parse_flow(resp.json())

    async def cancel_flow(self, flow_id: str) -> dict[str, Any]:
        resp = await self._client.post(f"/flows/{flow_id}/cancel")
        resp.raise_for_status()
        res: dict[str, Any] = resp.json()
        return res

    async def create_flow(
        self,
        goal: str,
        path: str = ".",
        content: str = "",
        tool_name: str = "workspace.write_text",
        idempotency_key: str = "cli-default",
        command: list[str] | None = None,
        correlation_id: str | None = None,
        read_after_write: bool = False,
        run_after_write: bool = False,
        run_command: list[str] | None = None,
        fix_after_run: bool = False,
        fix_content: str = "",
        fix_command: list[str] | None = None,
    ) -> dict[str, Any]:
        cid = correlation_id or str(uuid.uuid4())
        resp = await self._client.post(
            "/flows",
            json={
                "goal": goal,
                "path": path,
                "content": content,
                "tool_name": tool_name,
                "command": command or [],
                "read_after_write": read_after_write,
                "run_after_write": run_after_write,
                "run_command": run_command or [],
                "fix_after_run": fix_after_run,
                "fix_content": fix_content,
                "fix_command": fix_command or [],
            },
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Correlation-Id": cid,
            },
        )
        resp.raise_for_status()
        res: dict[str, Any] = resp.json()
        if "correlation_id" not in res:
            res["correlation_id"] = cid
        return res

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a free-text task via POST /tasks.

        Thin adapter over the existing flow creation — the Gateway reuses
        the same CreateTask + DurableQueue path as POST /flows."""
        body: dict[str, Any] = {
            "message": message,
            "conversation_id": conversation_id,
            "client": client,
        }
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post(
            "/tasks",
            json=body,
            headers={
                "Idempotency-Key": idempotency_key or f"task-{uuid.uuid4().hex[:12]}",
            },
        )
        resp.raise_for_status()
        res: dict[str, Any] = resp.json()
        return res

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> dict[str, Any]:
        """Отправить диалоговую реплику на Gateway через POST /api/v1/dialogue/turn."""
        payload: dict[str, Any] = {
            "text": text,
            "session_id": session_id,
            "channel": channel,
            "user_id": user_id,
        }
        if turn_id:
            payload["turn_id"] = turn_id
        headers = {"X-Turn-Id": turn_id} if turn_id else None
        # Dialogue turns include an LLM round-trip; the default 30s client
        # timeout is too tight for cold DeepSeek/OpenRouter and surfaces as
        # a false "Gateway недоступен" in Telegram/CLI.
        turn_timeout = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
        try:
            resp = await self._client.post(
                "/api/v1/dialogue/turn",
                json=payload,
                headers=headers,
                timeout=turn_timeout,
            )
            resp.raise_for_status()
            res: dict[str, Any] = resp.json()
            return res
        except httpx.HTTPStatusError as exc:
            detail: Any = _safe_error_detail(exc.response.text[:500])
            logger.error(
                "Gateway POST /api/v1/dialogue/turn вернул %d: %s",
                exc.response.status_code,
                detail,
            )
            raise GatewayHTTPError(
                f"Gateway ответил ошибкой {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.ConnectError as exc:
            raise GatewayConnectionError(
                "Gateway недоступен, попробуйте позже"
            ) from exc
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                "Время ожидания Gateway истекло"
            ) from exc

    async def list_commands(self, channel: str = "all") -> list[dict[str, object]]:
        """Получить единый реестр команд (Step 10)."""
        resp = await self._client.get(
            "/commands",
            params={"channel": channel},
        )
        resp.raise_for_status()
        return list(resp.json())

    async def health(self) -> dict[str, Any]:
        """Проверить доступность Gateway (GET /health)."""
        resp = await self._client.get("/health")
        resp.raise_for_status()
        return dict(resp.json())

    async def memory_list(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Прочитать единую память ядра."""
        params: dict[str, Any] = {"limit": str(limit)}
        if kind:
            params["kind"] = kind
        if query:
            params["query"] = query
        resp = await self._client.get("/api/v1/memory", params=params)
        resp.raise_for_status()
        return dict(resp.json())

    async def memory_create(
        self,
        content: str,
        *,
        kind: str = "fact",
        title: str = "",
        source: str = "user",
    ) -> dict[str, Any]:
        """Записать в единую память ядра."""
        resp = await self._client.post(
            "/api/v1/memory",
            json={"content": content, "kind": kind, "title": title, "source": source},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def memory_delete(self, entry_id: str) -> dict[str, Any]:
        """Удалить запись из единой памяти ядра."""
        resp = await self._client.delete(f"/api/v1/memory/{entry_id}")
        resp.raise_for_status()
        return dict(resp.json())

    async def session_info(self, session_id: str) -> dict[str, Any]:
        """Информация о сессии ядра."""
        resp = await self._client.get(f"/sessions/{session_id}")
        resp.raise_for_status()
        return dict(resp.json())

    async def reset_session(self, session_id: str) -> dict[str, Any]:
        """End the caller's conversation for this session (history + active flow)."""
        resp = await self._client.post(f"/sessions/{session_id}/reset")
        resp.raise_for_status()
        return dict(resp.json())

    async def session_history(self, session_id: str, limit: int = 100) -> dict[str, Any]:
        """История переписки сессии ядра."""
        resp = await self._client.get(
            f"/sessions/{session_id}/history",
            params={"limit": str(limit)},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def get_events(self, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/events",
            params={"after_seq": str(after_seq), "limit": str(limit)},
        )
        resp.raise_for_status()
        res: list[dict[str, Any]] = resp.json()
        return res

    def events_ws_url(self, after_seq: int = 0) -> str:
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/ws/events?token={self.token}&after_seq={after_seq}"

    async def connect_events(
        self,
        after_seq: int = 0,
        max_retries: int = 10,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> AsyncIterator[dict[str, Any]]:
        import websockets

        current_seq = after_seq
        seen_keys: set[tuple[str, int]] = set()
        retries = 0
        backoff = initial_backoff

        while True:
            try:
                events = await self.get_events(after_seq=current_seq)
                for ev in events:
                    seq = ev.get("seq", 0)
                    fid = ev.get("flow_id", "")
                    key = (fid, seq)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        if seq > current_seq:
                            current_seq = seq
                        yield ev
                retries = 0
                backoff = initial_backoff
            except (httpx.HTTPError, OSError) as exc:
                retries += 1
                if retries > max_retries:
                    raise ConnectionError(
                        f"Gateway unavailable at {self.base_url} (reason: {exc})"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            ws_url = self.events_ws_url(after_seq=current_seq)
            try:
                async with websockets.connect(ws_url, open_timeout=10) as ws:
                    retries = 0
                    backoff = initial_backoff
                    async for raw_msg in ws:
                        ws_ev: dict[str, Any] = json.loads(raw_msg)
                        seq = ws_ev.get("seq", 0)
                        fid = ws_ev.get("flow_id", "")
                        key = (fid, seq)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            if seq > current_seq:
                                current_seq = seq
                            yield ws_ev
            except (TimeoutError, websockets.WebSocketException, OSError) as exc:
                retries += 1
                if retries > max_retries:
                    raise ConnectionError(
                        f"Gateway unavailable at {self.base_url} (reason: {exc})"
                    ) from exc
                logger.warning("WS disconnected (%s). Reconnecting in %fs...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def decide_approval(
        self, approval_id: str, approve: bool
    ) -> ApprovalView:
        """Accept or reject a pending approval.

        POST /approvals/{id}/decision with body ``{"approve": bool}``.
        """
        resp = await self._client.post(
            f"/approvals/{approval_id}/decision",
            json={"approve": approve},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GatewayProtocolError("Gateway approval response must be an object")
        if "id" not in data:
            raise GatewayProtocolError("Gateway approval response is missing approval id")
        response_id = data["id"]
        if not isinstance(response_id, str) or not response_id or response_id != approval_id:
            raise GatewayProtocolError("Gateway approval id does not match request")
        if "decision" not in data:
            raise GatewayProtocolError("Gateway approval response is missing decision")
        decision = data["decision"]
        if decision not in {"APPROVED", "DENIED"}:
            raise GatewayProtocolError("Gateway returned an unknown approval decision")
        expected_decision = "APPROVED" if approve else "DENIED"
        if decision != expected_decision:
            raise GatewayProtocolError("Gateway approval decision does not match request")
        required = {"tool_name", "risk_level", "reason"}
        if not required.issubset(data):
            raise GatewayProtocolError("Malformed Gateway approval response")
        return ApprovalView(
            id=response_id,
            tool_name=str(data["tool_name"]),
            risk_level=str(data["risk_level"]),
            reason=str(data["reason"]),
            decision=decision,
            decided_by=str(data["decided_by"]) if data.get("decided_by") else None,
        )

    async def get_flow(self, flow_id: str) -> GatewayFlowView:
        resp = await self._client.get(f"/flows/{flow_id}")
        if resp.status_code == 404:
            raise GatewayFlowNotFoundError(flow_id)
        resp.raise_for_status()
        return self._parse_flow(resp.json())

    async def get_result(self, flow_id: str) -> GatewayFlowResult:
        """Read the owner-scoped, sanitized result projection for a flow."""
        resp = await self._client.get(f"/flows/{flow_id}/result")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GatewayProtocolError("Gateway result response must be an object")

        status = _parse_flow_status(data.get("status"))
        raw_artifacts = data.get("artifacts") or []
        if not isinstance(raw_artifacts, list):
            raise GatewayProtocolError("Gateway result artifacts must be a list")
        artifacts: list[GatewayResultArtifact] = []
        for raw in raw_artifacts:
            if not isinstance(raw, dict):
                raise GatewayProtocolError("Gateway result artifact must be an object")
            try:
                size = int(raw["size"])
                artifact = GatewayResultArtifact(
                    path=str(raw["path"]),
                    sha256=str(raw["sha256"]),
                    size=size,
                    verified=bool(raw.get("verified", False)),
                )
            except (KeyError, TypeError, ValueError):
                raise GatewayProtocolError("Malformed Gateway result artifact") from None
            if artifact.size < 0:
                raise GatewayProtocolError("Gateway result artifact size must not be negative")
            artifacts.append(artifact)

        terminal = bool(data.get("terminal", False))
        server_success = bool(data.get("success", False))
        result = GatewayFlowResult(
            flow_id=str(data.get("flow_id") or flow_id),
            status=status,
            terminal=terminal,
            success=(
                terminal
                and status is FlowStatus.DONE
                and server_success
            ),
            artifacts=tuple(artifacts),
            safe_result_text=_optional_text(data.get("safe_result_text")),
            stdout_preview=_optional_text(data.get("stdout_preview")),
            failure_reason=_optional_text(data.get("failure_reason")),
            completed_at=_optional_text(data.get("completed_at")),
            revision=_parse_revision(data.get("revision")),
        )
        if result.success and _presentable_result(result) is None:
            raise GatewayProtocolError(
                "Gateway reported a successful DONE result without any usable "
                "verified content"
            )
        return result


    async def get_replay(
        self,
        flow_id: str,
        actor: str | None = None,
        entity_type: str | None = None,
        from_dt: str | None = None,
        to_dt: str | None = None,
    ) -> dict[str, Any]:
        pairs = {"actor": actor, "entity_type": entity_type, "from_dt": from_dt, "to_dt": to_dt}
        params = {k: v for k, v in pairs.items() if v}
        resp = await self._client.get(f"/flows/{flow_id}/replay", params=params)
        resp.raise_for_status()
        res: dict[str, Any] = resp.json()
        return res

    async def get_replay_timeline(self, flow_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/flows/{flow_id}/replay/timeline")
        resp.raise_for_status()
        res: dict[str, Any] = resp.json()
        return res

    def progress_ws_url(self, flow_id: str) -> str:
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/flows/{flow_id}/progress?token={self.token}"

    async def stream_progress_ws(self, flow_id: str) -> AsyncIterator[dict[str, Any]]:
        import websockets

        url = self.progress_ws_url(flow_id)
        kwargs: dict[str, Any] = {"open_timeout": 10}
        if "127.0.0.1" in url or "localhost" in url or "::1" in url:
            kwargs["proxy"] = None

        async with websockets.connect(url, **kwargs) as ws:
            async for raw_msg in ws:
                message: dict[str, Any] = json.loads(raw_msg)
                yield message
                if message.get("type") in {"end", "error"}:
                    return

    async def wait_for_terminal(
        self,
        flow_id: str,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        cancel_event: asyncio.Event | None = None,
        final_read_grace: float = DEFAULT_FINAL_READ_GRACE,
    ) -> GatewayFlowView:
        """Poll until a terminal state and then fetch its authoritative result.

        One ``time.monotonic()`` deadline governs the *whole* wait: every
        awaited HTTP read is wrapped in the budget that is still left, so a slow
        or hanging Gateway can no longer make a ``timeout=0.005`` waiter block
        for seconds inside a socket read.  ``final_read_grace`` is the floor
        applied to the mandatory authoritative reads only (see
        ``DEFAULT_FINAL_READ_GRACE``); the total overshoot is bounded by a small
        multiple of it.

        Local cancellation only aborts this waiter.  It deliberately never calls
        the Gateway cancellation endpoint, so the durable flow continues unless
        the caller separately requests remote cancellation — but it is still
        folded into one final authoritative read, because a flow that became
        terminal while the caller was giving up must be reported as terminal
        rather than swallowed.
        """

        grace = max(0.0, float(final_read_grace))
        deadline = time.monotonic() + max(0.0, float(timeout))
        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        while True:
            try:
                view = await self._get_flow_bounded(flow_id, deadline, grace)
            except TimeoutError:
                if cancelled():
                    return await self._finish_local_cancellation(
                        flow_id, timeout, deadline, grace
                    )
                return await self._finish_deadline(flow_id, timeout, deadline, grace)

            if view.status in TERMINAL_FLOW_STATUSES:
                return await self._terminal_result(view, timeout, deadline, grace)

            if cancelled():
                return await self._finish_local_cancellation(
                    flow_id, timeout, deadline, grace
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await self._finish_deadline(flow_id, timeout, deadline, grace)

            sleep_for = min(max(0.0, poll_interval), remaining)
            if cancel_event is None:
                await asyncio.sleep(sleep_for)
                continue
            if sleep_for == 0:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=sleep_for)
            except TimeoutError:
                continue
            return await self._finish_local_cancellation(
                flow_id, timeout, deadline, grace
            )

    async def list_flows(
        self,
        conversation_id: str | FlowStatus | None = "",
        status: FlowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FlowView]:
        """List flows with optional filtering.

        GET /flows?status=X&limit=Y&offset=Z returns ``FlowListView``
        (``{"items": [...], "total": N}``). The method returns the
        ``items`` list; callers that need ``total`` can parse it themselves.
        """
        if isinstance(conversation_id, FlowStatus):
            status = conversation_id
            conversation_id = ""

        params: dict[str, str] = {
            "limit": str(max(1, min(limit, 200))),
            "offset": str(max(0, offset)),
        }
        if status is not None:
            params["status"] = status.value if isinstance(status, FlowStatus) else str(status)
        resp = await self._client.get("/flows", params=params)
        resp.raise_for_status()
        body = resp.json()
        items = body.get("items", []) if isinstance(body, dict) else body
        return [self._parse_flow(f) for f in items]

    async def list_approvals(
        self,
        status: str = "PENDING",
        limit: int = 50,
        offset: int = 0,
    ) -> ApprovalListView:
        """List approvals with optional status filter.

        GET /approvals?status=X&limit=Y&offset=Z returns ``ApprovalListView``.
        """
        params: dict[str, str] = {
            "status": status,
            "limit": str(max(1, min(limit, 200))),
            "offset": str(max(0, offset)),
        }
        resp = await self._client.get("/approvals", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GatewayProtocolError("Gateway approval list response must be an object")
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise GatewayProtocolError("Gateway approval list items must be a list")
        total = int(data.get("total", 0))
        items: list[ApprovalListEntry] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise GatewayProtocolError("Malformed Gateway approval list item")
            required_keys = {"id", "task_id", "tool_name", "risk_level", "reason", "created_at"}
            if not required_keys.issubset(item):
                raise GatewayProtocolError("Malformed Gateway approval list item")
            items.append(
                ApprovalListEntry(
                    id=str(item["id"]),
                    task_id=str(item["task_id"]),
                    tool_name=str(item["tool_name"]),
                    risk_level=str(item["risk_level"]),
                    reason=str(item["reason"]),
                    created_at=str(item["created_at"]),
                )
            )
        return ApprovalListView(items=items, total=total)

    async def get_approval(self, approval_id: str) -> ApprovalView:
        """Get a single approval by ID.

        GET /approvals/{id} returns ``ApprovalView``.
        """
        resp = await self._client.get(f"/approvals/{approval_id}")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GatewayProtocolError("Gateway approval response must be an object")
        if "id" not in data or data["id"] != approval_id:
            raise GatewayProtocolError("Gateway approval response missing or mismatched id")
        if "decision" not in data or not data["decision"]:
            raise GatewayProtocolError("Gateway approval response missing decision")
        decision = str(data["decision"])
        if decision not in {"PENDING", "APPROVED", "DENIED"}:
            raise GatewayProtocolError("Gateway returned unknown approval decision")
        required = {"tool_name", "risk_level", "reason"}
        if not required.issubset(data):
            raise GatewayProtocolError("Malformed Gateway approval response")
        return ApprovalView(
            id=str(data["id"]),
            tool_name=str(data["tool_name"]),
            risk_level=str(data["risk_level"]),
            reason=str(data["reason"]),
            decision=decision,
            decided_by=str(data["decided_by"]) if data.get("decided_by") else None,
        )

    async def stream_events(
        self, conversation_id: str, cursor: str = ""
    ) -> AsyncIterator[Any]:
        """SSE-стриминг событий."""
        params = {"conversation_id": conversation_id}
        if cursor:
            params["cursor"] = cursor
        async with self._client.stream(
            "GET", "/events/stream", params=params
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    # ── Internal ──────────────────────────────────────────────────────────

    async def _get_flow_bounded(
        self, flow_id: str, deadline: float, grace: float
    ) -> GatewayFlowView:
        """One flow GET, hard-bounded by what is left of the overall deadline."""

        res = await asyncio.wait_for(
            self.get_flow(flow_id), _read_budget(deadline, grace)
        )
        if isinstance(res, GatewayFlowView):
            return res
        return self._parse_flow(res)


    async def _terminal_result(
        self, view: GatewayFlowView, timeout: float, deadline: float, grace: float
    ) -> GatewayFlowView:
        """Validate an observed terminal state against ``/result``.

        A budget exhausted mid-validation is reported as a typed wait timeout:
        the terminal state was seen, but nothing may be returned as a success
        without the authoritative result behind it.
        """

        try:
            return await self._attach_terminal_result(
                view, deadline=deadline, grace=grace
            )
        except TimeoutError:
            raise GatewayWaitTimeoutError(view.flow_id, timeout) from None

    async def _finish_deadline(
        self, flow_id: str, timeout: float, deadline: float, grace: float
    ) -> GatewayFlowView:
        """Resolve the deadline race with one final authoritative read."""

        try:
            final_view = await self._get_flow_bounded(flow_id, deadline, grace)
        except TimeoutError:
            raise GatewayWaitTimeoutError(flow_id, timeout) from None
        if final_view.status in TERMINAL_FLOW_STATUSES:
            return await self._terminal_result(final_view, timeout, deadline, grace)
        raise GatewayWaitTimeoutError(flow_id, timeout)

    async def _finish_local_cancellation(
        self, flow_id: str, timeout: float, deadline: float, grace: float
    ) -> GatewayFlowView:
        """Fold local cancellation into one final authoritative read.

        No POST is ever issued here: remote cancellation stays an explicit,
        separate caller decision (``cancel()``).  If that read observes a
        terminal state, the flow really did finish and its validated result is
        returned; anything else — including a read that fails or runs out of
        budget — fails closed as a typed local cancellation.
        """

        try:
            view = await self._get_flow_bounded(flow_id, deadline, grace)
        except (TimeoutError, GatewayError, httpx.HTTPError) as exc:
            logger.info(
                "wait_for_terminal: final read after local cancellation failed "
                "for flow %s (%s)",
                flow_id[:12],
                type(exc).__name__,
            )
            raise GatewayWaitCancelledError(flow_id) from None
        if view.status in TERMINAL_FLOW_STATUSES:
            return await self._terminal_result(view, timeout, deadline, grace)
        raise GatewayWaitCancelledError(flow_id)

    async def _attach_terminal_result(
        self,
        view: GatewayFlowView,
        *,
        deadline: float | None = None,
        grace: float = DEFAULT_FINAL_READ_GRACE,
    ) -> GatewayFlowView:
        if deadline is None:
            result = await self.get_result(view.flow_id)
        else:
            result = await asyncio.wait_for(
                self.get_result(view.flow_id), _read_budget(deadline, grace)
            )

        if result.flow_id != view.flow_id:
            raise GatewayProtocolError("Gateway result flow_id does not match flow")
        if not result.terminal or result.status not in TERMINAL_FLOW_STATUSES:
            raise GatewayProtocolError("Gateway result is not terminal")
        if result.status is not view.status:
            raise GatewayProtocolError("Gateway flow and result statuses do not match")
        # Terminal states are absorbing and the revision only moves on task
        # transitions, so the result of an already-terminal flow must carry the
        # same revision that was just observed.  A conflict means the two reads
        # describe different states — reject instead of guessing which is fresh.
        view_revision = getattr(view, "revision", None)
        if (
            view_revision is not None
            and result.revision is not None
            and view_revision != result.revision
        ):
            raise GatewayProtocolError(
                "Gateway result revision does not match the observed flow revision"
            )

        succeeded = view.status is FlowStatus.DONE and result.success
        # ``get_result`` already rejected a success without usable content, so
        # a successful result always yields a non-empty presentation here.
        result_text = _presentable_result(result)
        failure_reason = result.failure_reason
        if not succeeded and failure_reason is None:
            failure_reason = (
                "verified result unavailable"
                if view.status is FlowStatus.DONE
                else f"flow ended with status {view.status.value}"
            )
        return replace(
            view,
            result=result_text if succeeded else None,
            error=None if succeeded else failure_reason,
        )

    @staticmethod
    def _parse_flow(data: dict[str, Any]) -> GatewayFlowView:
        """Map a Gateway ``TaskView``/``FlowSummary`` JSON body to ``FlowView``.

        The Gateway's actual response schema (``schemas.TaskView`` /
        ``FlowSummary``) uses ``id`` and ``goal`` — there is no ``flow_id``
        or ``title`` field anywhere in the Gateway API. Reading those meant
        every successful ``submit()`` produced an empty id and title (the
        "✅ Задача создана: **...** (``)" placeholder seen in production),
        regardless of what the task actually was. ``.get("flow_id"/"title")``
        is kept as a fallback only in case a future/alternate endpoint shape
        ever uses those names — it should never be the one that matches.
        """
        status = _parse_flow_status(
            data.get("status") or data.get("flow_status") or FlowStatus.QUEUED.value
        )

        steps = list(data.get("steps") or [])
        transitions = list(data.get("events") or data.get("transitions") or [])
        result = data.get("result")

        error = data.get("error")
        if error is None and status in TERMINAL_FLOW_STATUSES - {FlowStatus.DONE}:
            for transition in reversed(transitions):
                if isinstance(transition, dict) and transition.get("reason"):
                    error = str(transition["reason"])
                    break

        current_step = data.get("current_step")
        if current_step is None:
            current_step = next(
                (
                    str(step.get("title") or step.get("id") or "")
                    for step in steps
                    if isinstance(step, dict)
                    and step.get("status") in {"PENDING", "RUNNING"}
                ),
                None,
            )

        return GatewayFlowView(
            flow_id=str(data.get("id") or data.get("flow_id") or ""),
            conversation_id=str(data.get("conversation_id", "")),
            title=data.get("goal") or data.get("title") or "",
            status=status,
            progress=data.get("progress", 0),
            current_step=current_step,
            steps=steps,
            events=transitions,
            result=str(result) if result is not None else None,
            error=str(error) if error is not None else None,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            revision=_parse_revision(data.get("revision")),
        )


def _truncate_json(obj: dict[str, Any], max_len: int = 500) -> str:
    """Return a bounded fingerprint summary without logging raw request values."""

    serialized = json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)
    summary = {
        "bytes": len(serialized.encode()),
        "keys": sorted(str(key) for key in obj),
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "tool_name": str(obj.get("tool_name") or ""),
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _safe_error_detail(detail: object) -> dict[str, Any]:
    """Describe an HTTP error body without retaining echoed request values."""

    if not isinstance(detail, dict):
        return {"body_type": type(detail).__name__}
    raw_errors = detail.get("detail")
    if not isinstance(raw_errors, list):
        return {"body_type": "object", "key_count": len(detail)}
    error_types: list[str] = []
    for raw in raw_errors[:20]:
        if not isinstance(raw, dict):
            continue
        error_type = str(raw.get("type") or "validation_error")
        if len(error_type) > 64 or not all(
            character.isalnum() or character in "._-" for character in error_type
        ):
            error_type = "validation_error"
        error_types.append(error_type)
    return {"error_count": len(raw_errors), "types": sorted(set(error_types))}


def _parse_flow_status(value: object) -> FlowStatus:
    """Map a raw status string, never coercing an unknown one to a known state."""

    raw_status = str(value or FlowStatus.QUEUED.value)
    try:
        return FlowStatus(raw_status)
    except ValueError:
        # ``from None``: ``ValueError`` echoes the raw server value verbatim.
        raise GatewayProtocolError(
            f"Gateway returned unsupported flow status {_safe_token(raw_status)!r}"
        ) from None


def _parse_revision(value: object) -> int | None:
    """Preserve the raw authoritative revision; an absent one stays ``None``.

    Older (and mocked) payloads simply omit the field — those stay backward
    compatible and skip revision agreement.  A *present* but malformed revision
    is a protocol violation: silently treating it as "unknown" would disable the
    agreement check exactly when a body cannot be trusted.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayProtocolError("Gateway revision must be an integer")
    if value < 0:
        raise GatewayProtocolError("Gateway revision must not be negative")
    return value


def _read_budget(deadline: float, grace: float) -> float:
    """Budget for one read: what is left of the deadline, floored at ``grace``.

    The floor only ever applies once the deadline has already elapsed, so that
    the mandatory authoritative read can still be *issued* — it is what keeps
    "cancel/deadline must not hang on I/O" and "one final authoritative read"
    from contradicting each other.
    """

    return max(deadline - time.monotonic(), max(0.0, grace))


def _safe_token(value: str, max_len: int = 32) -> str:
    """Reduce a server-supplied token to a bounded, log/message-safe form."""

    cleaned = "".join(
        character
        for character in value
        if character.isalnum() or character in "._-"
    )[:max_len]
    return cleaned or "<unprintable>"


def _usable_text(value: str | None) -> str | None:
    """Return the text only if it carries something other than whitespace."""

    if value is None:
        return None
    return value if value.strip() else None


def _is_usable_artifact(artifact: GatewayResultArtifact) -> bool:
    """A verified, non-empty, identifiable artifact.

    A zero-byte "verified" artifact is exactly the malformed success shape the
    audit found, so it does not count as a usable result.
    """

    return (
        artifact.verified
        and artifact.size > 0
        and bool(artifact.path.strip())
        and bool(artifact.sha256.strip())
    )


def _artifact_summary(artifacts: tuple[GatewayResultArtifact, ...]) -> str | None:
    """Describe usable artifacts with metadata only (never file contents)."""

    usable = [artifact for artifact in artifacts if _is_usable_artifact(artifact)]
    if not usable:
        return None
    described = [
        f"{artifact.path} ({artifact.size} bytes, sha256:{artifact.sha256[:12]})"
        for artifact in usable[:_ARTIFACT_SUMMARY_LIMIT]
    ]
    if len(usable) > _ARTIFACT_SUMMARY_LIMIT:
        described.append(f"+{len(usable) - _ARTIFACT_SUMMARY_LIMIT} more")
    return "verified artifacts: " + ", ".join(described)


def _presentable_result(result: GatewayFlowResult) -> str | None:
    """The safe text a caller can actually show, or ``None`` if there is none.

    Order matters: verifier-approved read-back first, then the sanitized stdout
    preview, and only then a metadata-only artifact summary — so a success
    backed purely by a verified artifact still returns a non-empty result
    instead of the ``result=None`` + ``error=None`` ambiguity found by review.
    """

    return (
        _usable_text(result.safe_result_text)
        or _usable_text(result.stdout_preview)
        or _artifact_summary(result.artifacts)
    )


def _optional_text(value: object | None) -> str | None:
    return None if value is None else str(value)
