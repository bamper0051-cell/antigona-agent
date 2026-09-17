from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from antigona.config import Settings
from antigona.gateway.api import create_gateway_app
from antigona.models import TaskState
from antigona.repository import CreateTask, TaskRepository

TOKENS = {"admin-token": "admin", "alice-token": "owner-alice"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        f"sqlite:///{tmp_path / 'db.sqlite'}",
        tmp_path / "workspace",
        dev_tokens=TOKENS,
        sandbox_backend="inprocess",
        test_mode=True,
    )


def _app(tmp_path: Path) -> Any:
    app = create_gateway_app(_settings(tmp_path))
    app.state.database.create_all()
    return app


def _auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _request(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, url, **kwargs)


def call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request(app, method, url, **kwargs))


def _create_flow(
    app: Any,
    owner_id: str = "admin",
    goal: str = "Test replay integration",
    idempotency_key: str = "int-replay-1",
    transitions: bool = False,
) -> str:
    with app.state.database.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                owner_id=owner_id,
                goal=goal,
                path="test.txt",
                content="data",
                idempotency_key=idempotency_key,
            )
        )
        if transitions:
            repo.transition(task, TaskState.QUEUED, reason="queued", actor="gateway")
            repo.transition(task, TaskState.PLANNING, reason="planning", actor="worker")
        return task.id


def test_replay_endpoint_returns_200(tmp_path: Path) -> None:
    """GET /flows/{id}/replay → 200, valid JSON."""
    app = _app(tmp_path)
    flow_id = _create_flow(app)
    resp = call(app, "GET", f"/flows/{flow_id}/replay", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == flow_id
    assert data["goal"] == "Test replay integration"


def test_replay_endpoint_owner_isolation(tmp_path: Path) -> None:
    """GET /flows/{id}/replay with wrong owner → 404."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, owner_id="owner-alice", idempotency_key="int-replay-owner-1")

    resp = call(app, "GET", f"/flows/{flow_id}/replay", headers=_auth("alice-token"))
    assert resp.status_code == 200

    resp = call(app, "GET", f"/flows/{flow_id}/replay", headers=_auth())
    assert resp.status_code == 404


def test_replay_endpoint_not_found(tmp_path: Path) -> None:
    """GET /flows/nonexistent/replay → 404."""
    app = _app(tmp_path)
    resp = call(app, "GET", "/flows/nonexistent-flow/replay", headers=_auth())
    assert resp.status_code == 404


def test_replay_endpoint_filter_actor(tmp_path: Path) -> None:
    """GET /flows/{id}/replay?actor=gateway → only gateway transitions."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-replay-filter-1", transitions=True)
    resp = call(
        app, "GET", f"/flows/{flow_id}/replay",
        params={"actor": "gateway"}, headers=_auth(),
    )
    assert resp.status_code == 200
    for tr in resp.json()["transitions"]:
        assert tr["actor"] == "gateway"


def test_replay_endpoint_timeline(tmp_path: Path) -> None:
    """GET /flows/{id}/replay/timeline → 200, entries present."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-replay-timeline-1", transitions=True)
    resp = call(app, "GET", f"/flows/{flow_id}/replay/timeline", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert len(data["entries"]) >= 1
    for entry in data["entries"]:
        assert "type" in entry
        assert "timestamp" in entry
        assert "description" in entry


def test_replay_without_auth(tmp_path: Path) -> None:
    """GET /flows/{id}/replay without auth → 401."""
    app = _app(tmp_path)
    flow_id = _create_flow(app, idempotency_key="int-replay-auth-1")
    resp = call(app, "GET", f"/flows/{flow_id}/replay")
    assert resp.status_code == 401
