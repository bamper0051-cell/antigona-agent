"""LEGACY (v0.2.0) — НЕ используется активным кодом (Step 2 манифеста).

Канонический Gateway — antigona.gateway.api (create_gateway_app).
Этот модуль живёт только для тестов (tests/integration/test_api.py,
scripts/e2e_docker.py). Не добавлять новые consumers.
"""

import hashlib
import hmac
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .filesystem import (
    DockerSandboxBackend,
    InProcessTestBackend,
    SandboxBackend,
    WorkspaceFileTool,
    WorkspaceViolation,
    validate_relative_path,
)
from .queue import DurableQueue
from .repository import CreateTask, IdempotencyConflict, TaskNotFound, TaskRepository
from .sandbox.runner import resolve_runtime
from .schemas import ApprovalDecision, ApprovalView, TaskCreate, TaskView


def create_app(settings: Settings|None=None)->FastAPI:
    resolved=settings or Settings.from_env(); database=Database(resolved.database_url)
    backend: SandboxBackend
    if resolved.sandbox_backend=="inprocess": backend=InProcessTestBackend(resolved.workspace,test_mode=resolved.test_mode)
    elif resolved.sandbox_backend=="docker": backend=DockerSandboxBackend(resolved.workspace,resolved.docker_image,resolve_runtime(resolved.sandbox_runtime))
    else: raise RuntimeError("unknown sandbox backend")
    tool=WorkspaceFileTool(backend,resolved.tool_timeout_seconds); token_hashes=resolved.token_hashes()

    @asynccontextmanager
    async def lifespan(_:FastAPI)->AsyncIterator[None]: database.create_all(); yield
    app=FastAPI(title="Антигона Gateway",version="0.2.0",lifespan=lifespan); app.state.database=database; app.state.tool=tool
    def get_session()->Iterator[Session]: yield from database.session()
    def owner(authorization:Annotated[str|None,Header(alias="Authorization")]=None)->str:
        if not authorization or not authorization.startswith("Bearer "): raise HTTPException(401,"bearer token required")
        digest=hashlib.sha256(authorization[7:].encode()).hexdigest()
        for known,value in token_hashes.items():
            if hmac.compare_digest(digest,known): return value
        raise HTTPException(401,"invalid bearer token")
    def load(task_id:str,owner_id:str,session:Session)->tuple[TaskRepository,Any]:
        repository=TaskRepository(session)
        try: return repository,repository.get(task_id,owner_id)
        except TaskNotFound as exc: raise HTTPException(404,"task not found") from exc

    @app.get("/health")
    def health()->dict[str,str]: return {"status":"ok","sandbox":resolved.sandbox_backend}
    @app.post("/tasks",response_model=TaskView,status_code=201)
    def create_task(body:TaskCreate,response:Response,idempotency_key:Annotated[str,Header(min_length=1,alias="Idempotency-Key")],owner_id:Annotated[str,Depends(owner)],session:Annotated[Session,Depends(get_session)])->TaskView:
        try: validate_relative_path(resolved.workspace,body.path)
        except WorkspaceViolation as exc: raise HTTPException(422,str(exc)) from exc
        if body.tool_name=="sandbox.shell" and not body.command: raise HTTPException(422,"shell command is required")
        try: task,created=TaskRepository(session).create(CreateTask(owner_id,body.goal,body.path,body.content,idempotency_key,body.tool_name,tuple(body.command)))
        except IdempotencyConflict as exc: raise HTTPException(409,str(exc)) from exc
        if not created: response.status_code=200
        if created: DurableQueue(session).enqueue(task)
        return TaskView.model_validate(TaskRepository(session).get(task.id))
    @app.get("/tasks/{task_id}",response_model=TaskView)
    def get_task(task_id:str,owner_id:Annotated[str,Depends(owner)],session:Annotated[Session,Depends(get_session)])->TaskView: return TaskView.model_validate(load(task_id,owner_id,session)[1])
    @app.post("/tasks/{task_id}/run",response_model=TaskView)
    def run_task(task_id:str,owner_id:Annotated[str,Depends(owner)],session:Annotated[Session,Depends(get_session)])->TaskView:
        _,task=load(task_id,owner_id,session)
        DurableQueue(session).enqueue(task)
        return TaskView.model_validate(TaskRepository(session).get(task.id))
    @app.post("/tasks/{task_id}/cancel",response_model=TaskView)
    def cancel_task(task_id:str,owner_id:Annotated[str,Depends(owner)],session:Annotated[Session,Depends(get_session)])->TaskView:
        repository,task=load(task_id,owner_id,session); return TaskView.model_validate(repository.cancel(task))
    @app.post("/tasks/{task_id}/approvals/{approval_id}",response_model=ApprovalView)
    def decide(task_id:str,approval_id:str,body:ApprovalDecision,owner_id:Annotated[str,Depends(owner)],session:Annotated[Session,Depends(get_session)])->ApprovalView:
        repository,task=load(task_id,owner_id,session)
        try: approval=repository.decide_approval(task,approval_id,owner_id,body.approve)
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
        DurableQueue(session).enqueue(repository.get(task.id))
        return ApprovalView.model_validate(approval)
    return app


def __getattr__(name: str) -> Any:
    """Lazily expose the legacy ASGI ``app`` (``uvicorn antigona.main:app``).

    Importing this module must have NO side effects.  Building the app probes
    Docker and fail-closed REFUSES when gVisor (``runsc``) is unavailable, so an
    eager module-level ``app = create_app()`` made merely *importing* this
    module abort on any host without gVisor (e.g. a CI runner).  The fail-closed
    behaviour is preserved — it now triggers when the app is actually built, not
    at import time.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
