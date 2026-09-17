from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from antigona.config import Settings
from antigona.database import Database
from antigona.gateway.api import create_gateway_app
from antigona.main import create_app
from antigona.orchestration import OrchestrationStore

TOKENS={"alice-token":"alice","bob-token":"bob"}
def settings(tmp_path:Path,backend:str="inprocess")->Settings: return Settings(f"sqlite:///{tmp_path/'db.sqlite'}",tmp_path/"workspace",TOKENS,backend,test_mode=True)
def auth(token:str="alice-token")->dict[str,str]: return {"Authorization":f"Bearer {token}"}
async def request(app:Any,method:str,url:str,**kwargs:Any)->httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client: return await client.request(method,url,**kwargs)
def call(app:Any,method:str,url:str,**kwargs:Any)->httpx.Response: return asyncio.run(request(app,method,url,**kwargs))
def create(app:Any,key:str="k",payload:dict[str,str]|None=None)->httpx.Response: return call(app,"POST","/tasks",headers={**auth(),"Idempotency-Key":key},json=payload or {"goal":"g","path":"x.txt","content":"value"})


def test_gateway_only_enqueues_durable_work(tmp_path:Path)->None:
    app=create_app(settings(tmp_path)); task=create(app).json()
    assert task["status"]=="QUEUED" and task["artifacts"]==[]
    assert not (tmp_path/"workspace/x.txt").exists()


def test_authn_owner_isolation_and_early_path_validation(tmp_path:Path)->None:
    app=create_app(settings(tmp_path)); task=create(app).json()
    assert call(app,"GET",f"/tasks/{task['id']}").status_code==401
    assert call(app,"GET",f"/tasks/{task['id']}",headers=auth("bob-token")).status_code==404
    bad=create(app,"bad",{"goal":"g","path":"../escape","content":"x"})
    assert bad.status_code==422 and not (tmp_path/"escape").exists()


def test_idempotency_conflict(tmp_path:Path)->None:
    app=create_app(settings(tmp_path)); assert create(app,"same").status_code==201
    assert create(app,"same").status_code==200
    conflict=create(app,"same",{"goal":"other","path":"y","content":"z"})
    assert conflict.status_code==409


def test_cancellation_is_sticky_across_restart(tmp_path:Path)->None:
    configured=settings(tmp_path); first=create_app(configured); task=create(first,"cancel-key").json()
    cancelled=call(first,"POST",f"/tasks/{task['id']}/cancel",headers=auth()).json(); assert cancelled["status"]=="CANCELLED"
    restarted=create_app(configured); rerun=call(restarted,"POST",f"/tasks/{task['id']}/run",headers=auth()).json()
    assert rerun["status"]=="CANCELLED" and not (tmp_path/"workspace/x.txt").exists()


def test_goal_api_persists_structured_autonomy_ingress(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    app = create_gateway_app(configured)
    app.state.database.create_all()
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/goals" and "POST" in getattr(route, "methods", set())
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    response = endpoint(
        {
            "objective": "fix the bug",
            "workspace": str(workspace),
            "mutation_required": True,
            "test_command": ["pytest", "-q"],
            "acceptance_criteria": ["tests pass"],
        },
        "alice",
    )
    goal = OrchestrationStore(Database(configured.database_url).session_factory).get_goal(
        response["goal_id"]
    )
    assert goal is not None
    assert goal.workspace == str(workspace.resolve())
    assert goal.mutation_required is True
    assert goal.test_command == ["pytest", "-q"]
    with pytest.raises(HTTPException) as invalid:
        endpoint(
            {"objective": "x", "workspace": str(workspace), "mutation_required": "true"},
            "alice",
        )
    assert invalid.value.status_code == 422
    with pytest.raises(HTTPException, match="protected") as protected:
        endpoint(
            {
                "objective": "x",
                "workspace": str(Path(__file__).resolve().parents[2]),
                "mutation_required": True,
            },
            "alice",
        )
    assert protected.value.status_code == 422
