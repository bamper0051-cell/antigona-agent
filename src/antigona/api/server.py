"""API Server — FastAPI HTTP server with SSE streaming, JSON-RPC over stdin,
and model introspection.

LEGACY (v1.0.0): не используется активным кодом (Step 2 манифеста).
Жив только для тестов (tests/integration/test_api_server_steer_idor.py).
Канонический путь — antigona.gateway.api.

Security (Stage 1 / AUDIT-C02): every non-liveness route is owner-scoped with
``Depends(_require_owner)`` — ``GET /api/model/options`` (provider/model
disclosure) and ``POST /v1/chat/completions`` (unauthenticated LLM proxy) are
both gated; one MUST NOT be opened again on the "legacy, so anything goes"
rationale.  The JSON-RPC ``execute_tool``/``chat`` methods require the same
owner credential.  ``GET /health`` stays open on purpose (liveness probe).
The HTTP server default bind is loopback (``127.0.0.1``); exposing it on all
interfaces is an explicit opt-in via ``--host 0.0.0.0``.

Provides:

  - ``GET /health`` — liveness probe (intentionally unauthenticated).
  - ``GET /api/model/options`` — list available models / providers (owner-scoped).
  - ``POST /v1/chat/completions`` — OpenAI-compatible streaming endpoint (owner-scoped).
  - JSON-RPC through stdin (for Agent Communication Protocol (ACP)-style integration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from antigona.conversation.engine import ConversationEngine
from antigona.schemas import SteerFlowRequest
from antigona.tools.action_executor import ActionExecutor
from antigona.tools.registry import ToolRegistry, register_builtins

logger = logging.getLogger(__name__)

# ─── Models ────────────────────────────────────────────────────────────────────


class SteerFlowResponse(BaseModel):
    status: str = "ok"
    flow_id: str
    message: str


def _require_owner(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    """Stage 1 security fix: every flow-mutating route in this legacy app must
    be owner-scoped, mirroring the canonical Gateway's Depends(owner). Without
    this, an unauthenticated caller could steer a foreign active flow."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "bearer token required")
    owner_id = _owner_id_from_token(authorization[7:])
    if owner_id is None:
        raise HTTPException(401, "invalid bearer token")
    return owner_id


def _owner_id_from_token(raw_token: str) -> str | None:
    """Resolve an owner id from a raw token, or ``None`` (fail-closed).

    The single authority behind both the HTTP bearer dependency
    (``_require_owner``) and the JSON-RPC owner credential: the same
    ``Settings.from_env().dev_tokens`` set and the same constant-time
    ``hmac.compare_digest`` over the SHA-256 digest.  An empty or unknown
    token yields ``None`` so every caller fails closed.
    """
    import hashlib
    import hmac

    if not raw_token:
        return None
    from antigona.config import Settings

    settings = Settings.from_env()
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    for known, value in (settings.dev_tokens or {}).items():
        token_digest = hashlib.sha256(known.encode()).hexdigest()
        if hmac.compare_digest(digest, token_digest):
            return str(value)
    return None



# ─── App factory ───────────────────────────────────────────────────────────────


def create_app(
    engine: ConversationEngine | None = None,
    executor: ActionExecutor | None = None,
    registry: ToolRegistry | None = None,
    dialogue_engine: Any | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        engine: A *ConversationEngine* instance.  A default one is created if
            omitted.
        executor: An *ActionExecutor* instance.  A default one is created if
            omitted.
        registry: A *ToolRegistry* instance.  Built-in tools are registered if
            one is provided.
        dialogue_engine: Legacy parameter, kept for shim/test callers. Stage 1:
            this app does NOT construct or own a DialogueEngine — the canonical
            Gateway owns the single server core.

    Returns:
        A configured *FastAPI* app.
    """
    app = FastAPI(
        title="Antigona API",
        version="1.0.0",
        description="Antigona autonomous-agent HTTP / SSE gateway",
    )

    # State
    app.state.engine = engine or ConversationEngine()
    app.state.executor = executor or ActionExecutor()

    # Stage 1: dialogue/turn is owned ONLY by the canonical Gateway
    # (antigona.gateway). This app must NOT construct its own DialogueEngine —
    # thin clients route through the single server core. If a dialogue_engine
    # argument is passed (legacy tests/shim callers), it is stored for
    # compatibility but never used to answer turns here.
    app.state.dialogue_engine = dialogue_engine

    if registry is not None:
        register_builtins(registry)
    app.state.registry = registry or ToolRegistry()

    # ── Routes ─────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness probe."""
        return JSONResponse(
            {
                "status": "ok",
                "service": "antigona-api",
                "version": "1.0.0",
            }
        )

    # Stage 1: /api/v1/dialogue/turn is owned ONLY by the canonical Gateway
    # (antigona.gateway, port 8090). This legacy app does not expose it and
    # must not construct its own DialogueEngine. Tests that need a turn
    # endpoint must target the Gateway app (antigona.gateway.api).

    @app.post("/flows/{flow_id}/steer", response_model=SteerFlowResponse)
    async def steer_flow(
        flow_id: str,
        request: SteerFlowRequest,
        owner_id: str = Depends(_require_owner),
    ) -> SteerFlowResponse:
        """Steer an active flow with user input/instruction.

        Owner-scoped (Stage 1 security fix): only an authenticated owner token
        may steer; the fallback path filters by owner_id so a caller can never
        inject a message into another owner's active flow.
        Checks existence and status of the task (allowed only in WAITING_APPROVAL / RUNNING).
        Returns 404 if not found, 400 if status is not WAITING_APPROVAL or RUNNING.
        """
        task_mgr = getattr(app.state, "task_manager", None)
        if task_mgr is not None:
            task = await task_mgr.get_task(flow_id)
            if task is None:
                raise HTTPException(status_code=404, detail="flow not found")
            # Owner-scoping: never allow steering another owner's flow. The
            # legacy in-memory Task has no owner column; when it does expose
            # one (or wraps a TaskFlow), enforce equality.
            task_owner = getattr(task, "owner_id", None) or getattr(task, "owner_user_id", None)
            if task_owner is not None and str(task_owner) != owner_id:
                raise HTTPException(status_code=403, detail="flow belongs to another owner")
            status_upper = str(task.status).upper()
            if status_upper not in {"WAITING_APPROVAL", "RUNNING", "WAITING_CONFIRMATION"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"Steering not allowed for flow in status {task.status}",
                )
            await task_mgr.steer_task(flow_id, request.message)
            return SteerFlowResponse(status="ok", flow_id=flow_id, message=request.message)

        from sqlalchemy import select

        from antigona.config import Settings
        from antigona.database import Database
        from antigona.models import TaskFlow

        database = getattr(app.state, "database", None)
        if database is None:
            settings = Settings.from_env()
            database = Database(settings.database_url)

        with database.session_factory() as session:
            # Owner-scoped lookup: a caller can only steer a flow they own.
            # Returning 404 (not 403) for a foreign flow keeps existence
            # private and matches the canonical Gateway's fail-closed load().
            flow = session.scalar(
                select(TaskFlow).where(
                    TaskFlow.id == flow_id,
                    TaskFlow.owner_id == owner_id,
                )
            )
            if flow is None:
                raise HTTPException(status_code=404, detail="flow not found")

            status_upper = str(flow.status).upper()
            if status_upper not in {"WAITING_APPROVAL", "RUNNING"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"Steering not allowed for flow in status {flow.status}",
                )

            tool_args = dict(flow.tool_arguments or {})
            steer_list = list(tool_args.get("steer_messages") or [])
            steer_list.append(request.message)
            tool_args["steer_messages"] = steer_list
            flow.tool_arguments = tool_args
            session.commit()

        return SteerFlowResponse(status="ok", flow_id=flow_id, message=request.message)

    @app.get("/api/model/options")
    async def model_options(
        owner_id: str = Depends(_require_owner),
    ) -> JSONResponse:
        """List available models / providers.

        Owner-scoped (Stage 1 security fix, AUDIT-C02/S3): the provider/model
        inventory is owner-only information disclosure, so it is gated by the
        same owner credential as the mutating routes.  Scans the provider
        directory and returns a list of options.
        """
        models: list[dict[str, Any]] = []
        try:
            from antigona.tools.provider_switcher import get_available_providers

            provider_list = get_available_providers()
            for p in provider_list:
                models.append(
                    {
                        "id": p.get("name", "unknown"),
                        "object": "model",
                        "owned_by": p.get("display_name", "antigona"),
                        "model": p.get("model", ""),
                        "status": p.get("status", ""),
                    }
                )
        except Exception as exc:
            logger.warning("Could not list providers: %s", exc)
            models = [
                {"id": "default", "object": "model", "owned_by": "antigona"},
            ]

        return JSONResponse({"object": "list", "data": models})

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        owner_id: str = Depends(_require_owner),
    ) -> StreamingResponse:
        """OpenAI-compatible streaming chat completions endpoint.

        Owner-scoped (Stage 1 security fix, AUDIT-C02/S1): this endpoint spends
        the owner's provider keys and quota, so an unauthenticated caller must
        not reach it.  The request is answered for the authenticated owner only.

        Request body (JSON)::

            {
                "model": "default",
                "messages": [{"role": "user", "content": "..."}],
                "stream": true
            }

        Returns an SSE stream of ``data: {...}`` chunks.
        """
        body = await request.json()
        model = body.get("model", "default")
        messages: list[dict[str, str]] = body.get("messages", [])
        engine: ConversationEngine = app.state.engine

        # Build input text from messages
        user_text = "\n".join(
            m["content"] for m in messages if m.get("role") in ("user", "system")
        )

        async def _generate() -> AsyncGenerator[str, None]:
            # Yield initial chunk
            yield f"data: {json.dumps({'id': 'antigona-' + model, 'object': 'chat.completion.chunk', 'choices': [{'delta': {'role': 'assistant'}, 'index': 0}]})}\n\n"

            # Generate response
            try:
                response = engine.reply(user_text)
                # Simulate token-by-token for SSE
                words = response.split(" ")
                for i, word in enumerate(words):
                    token = word + (" " if i < len(words) - 1 else "")
                    chunk = {
                        "id": f"antigona-{model}",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "delta": {"content": token},
                                "index": 0,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.02)  # simulate streaming delay
            except Exception as exc:
                yield f"data: {json.dumps({'error': {'message': str(exc)}})}\n\n"

            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# ═══════════════════════════════════════════════════════════════════════════════
# JSON-RPC over stdin  (Agent Communication Protocol (ACP)-style)
# ═══════════════════════════════════════════════════════════════════════════════

_RPC_METHODS: dict[str, Any] = {}


def register_rpc(method: str) -> Any:
    """Decorator that registers a JSON-RPC method handler."""

    def decorator(fn: Any) -> Any:
        _RPC_METHODS[method] = fn
        return fn

    return decorator


async def _handle_rpc_request(
    request: dict[str, Any],
    engine: ConversationEngine,
    executor: ActionExecutor,
    registry: ToolRegistry,
) -> dict[str, Any]:
    """Handle a single JSON-RPC 2.0 request.

    Built-in methods:

      - ``ping`` — returns ``"pong"`` (unauthenticated read-only).
      - ``chat`` — send a message to the conversation engine (owner credential
        required: spends provider keys).
      - ``execute_tool`` — run a tool by name via the registry (owner credential
        required: ``params.owner_token``; fails closed without it).
      - ``list_tools`` — list registered tools (unauthenticated read-only).
      - ``health`` — health check (unauthenticated read-only).
    """
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    # Stage 1 security fix (AUDIT-C02/S5): JSON-RPC methods that mutate state or
    # run a registry tool are owner-scoped exactly like the HTTP routes.  Before
    # this, ``execute_tool`` dispatched ANY registry tool with no credential at
    # all (it inherits every registry gate, but nothing authenticated the
    # caller).  The credential is validated by the same authority as
    # ``_require_owner`` (env dev-token set + constant-time digest compare); a
    # missing/invalid token is a JSON-RPC error object, never a bare exception.
    # ``ping``/``health``/``list_tools`` stay read-only and unauthenticated.
    if method in {"execute_tool", "chat"}:
        rpc_params = params if isinstance(params, dict) else {}
        rpc_token = str(rpc_params.get("owner_token") or rpc_params.get("token") or "")
        if _owner_id_from_token(rpc_token) is None:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        f"{method} requires a valid owner credential "
                        "(params.owner_token)"
                    ),
                },
                "id": req_id,
            }

    try:
        if method == "ping":
            result: Any = "pong"
        elif method == "chat":
            text = params.get("text", "")
            response = engine.reply(text)
            result = {"response": response}
        elif method == "execute_tool":
            tool_name = params.get("tool", "")
            args = params.get("args", {})
            result_raw = await registry.dispatch(tool_name, **args)
            # If it's already a JSON string, parse it for clean output
            result = json.loads(result_raw)
        elif method == "list_tools":
            tools = registry.list()
            # ``registry.list()`` is heterogeneous: new-style descriptor tools
            # expose ``.toolset``/``.schema`` directly, while ToolABC contract
            # tools only expose ``.spec`` (``.spec.category`` / ``.spec
            # .input_schema``).  Mirror registry.list()'s guarded dual-type
            # pattern so a contract object can never raise AttributeError here
            # and turn the whole listing into a -32603 error.
            result = []
            for t in tools:
                spec = getattr(t, "spec", None)
                toolset = getattr(t, "toolset", None)
                if toolset is None and spec is not None:
                    category = getattr(spec, "category", None)
                    toolset = getattr(category, "value", None)
                if not isinstance(toolset, str):
                    toolset = "" if toolset is None else str(toolset)
                schema = getattr(t, "schema", None)
                if schema is None and spec is not None:
                    schema = getattr(spec, "input_schema", None)
                result.append(
                    {
                        "name": getattr(t, "name", None),
                        "toolset": toolset,
                        "schema": schema if isinstance(schema, dict) else {},
                    }
                )
        elif method == "health":
            result = {"status": "ok", "version": "1.0.0"}
        else:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": req_id,
            }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": str(exc)},
            "id": req_id,
        }

    return {"jsonrpc": "2.0", "result": result, "id": req_id}


_READ_BUF = ""


async def _read_rpc_line() -> str:
    """Read one JSON-RPC request line from stdin.

    Supports both newline-delimited JSON and piped input.
    """
    global _READ_BUF
    loop = asyncio.get_event_loop()

    while "\n" not in _READ_BUF:
        data = await loop.run_in_executor(None, sys.stdin.buffer.read, 4096)
        if not data:
            raise EOFError("stdin closed")
        _READ_BUF += data.decode("utf-8", errors="replace")

    line, _READ_BUF = _READ_BUF.split("\n", 1)
    return line.strip()


async def run_rpc_stdin(
    engine: ConversationEngine | None = None,
    executor: ActionExecutor | None = None,
    registry: ToolRegistry | None = None,
) -> None:
    """Run JSON-RPC server reading from stdin and writing to stdout.

    Each line is a JSON-RPC 2.0 request.  Each response is written as a
    single JSON line to stdout.

    This is the Agent Communication Protocol (ACP)-compatible integration point.
    """
    engine = engine or ConversationEngine()
    executor = executor or ActionExecutor()
    registry = registry or ToolRegistry()
    register_builtins(registry)

    logger.info("JSON-RPC stdin server started (ACP-style)")

    while True:
        try:
            line = await _read_rpc_line()
        except EOFError:
            break

        if not line:
            continue

        try:
            request: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
        else:
            response = await _handle_rpc_request(request, engine, executor, registry)

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    engine: ConversationEngine | None = None,
    executor: ActionExecutor | None = None,
    registry: ToolRegistry | None = None,
) -> None:
    """Run the FastAPI HTTP server.

    Args:
        host: Bind address (default: 127.0.0.1 loopback; pass "0.0.0.0" to
            expose the server on every interface explicitly).
        port: Bind port.
        engine: Optional *ConversationEngine*.
        executor: Optional *ActionExecutor*.
        registry: Optional *ToolRegistry*.
    """
    import uvicorn

    app = create_app(engine=engine, executor=executor, registry=registry)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    """CLI entry point for the API server.

    Usage::

        antigona-api                        # run HTTP server on :8765
        antigona-api --rpc-stdin            # run JSON-RPC over stdin
        antigona-api --host 0.0.0.0 --port 8080
    """
    import argparse

    parser = argparse.ArgumentParser(description="Antigona API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1 loopback; pass 0.0.0.0 to expose on all interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--rpc-stdin", action="store_true", help="Run JSON-RPC over stdin instead of HTTP")
    args = parser.parse_args()

    if args.rpc_stdin:
        asyncio.run(run_rpc_stdin())
    else:
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
