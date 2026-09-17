"""Regression-тест IDOR-фикса steer в legacy api.server (review v3).

PR #5 дважды блокировался review из-за отсутствия owner-scoping в
POST /flows/{flow_id}/steer (api/server.py). Этот тест защищает фикс:
- свой flow → steer работает (200);
- чужой flow → 404 (существование скрыто, IDOR закрыт);
- без токена → 401.

Аналогичен test_authn_owner_isolation_and_early_path_validation
(tests/integration/test_api.py), но бьёт именно по api.server.steer_flow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from antigona.api.server import create_app
from antigona.config import Settings
from antigona.database import Database
from antigona.repository import CreateTask, TaskRepository

TOKENS = {"alice-token": "alice", "bob-token": "bob"}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        f"sqlite:///{tmp_path/'db.sqlite'}", tmp_path / "workspace", TOKENS,
        "inprocess", test_mode=True,
    )


def auth(token: str = "alice-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def request(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, url, **kwargs)


def call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(request(app, method, url, **kwargs))


def _create_flow(database: Database, owner: str, flow_id: str) -> str:
    """Создать TaskFlow в БД и перевести в RUNNING (статус, где steer разрешён)."""
    from sqlalchemy import select

    from antigona.models import TaskFlow

    with database.session_factory() as session:
        repository = TaskRepository(session)
        task, created = repository.create(
            CreateTask(
                owner_id=owner,
                goal="create report",
                path="report.txt",
                content="report body",
                idempotency_key=f"idem-{flow_id}",
                tool_name="workspace.write_text",
                command=(),
            ),
            correlation_id="corr-" + flow_id,
        )
        assert created
        # Перевести в состояние, в котором steer разрешён.
        tf = session.scalar(select(TaskFlow).where(TaskFlow.id == task.id))
        assert tf is not None
        tf.status = "RUNNING"
        session.commit()
        return task.id


@pytest.fixture()
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Database]:
    monkeypatch.setenv(
        "ANTIGONA_DEV_TOKENS",
        "alice-token:alice,bob-token:bob",
    )
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", f"sqlite:///{tmp_path/'db.sqlite'}")
    settings = make_settings(tmp_path)
    database = Database(settings.database_url)
    database.create_all()
    app = create_app()
    app.state.database = database
    return app, database


def test_steer_requires_authentication(env: tuple[Any, Database]) -> None:
    app, database = env
    flow_id = _create_flow(database, "alice", "a1")
    response = call(
        app, "POST", f"/flows/{flow_id}/steer", json={"message": "change"},
    )
    assert response.status_code == 401


def test_steer_own_flow_allowed(env: tuple[Any, Database]) -> None:
    app, database = env
    flow_id = _create_flow(database, "alice", "a2")
    response = call(
        app, "POST", f"/flows/{flow_id}/steer",
        headers=auth("alice-token"), json={"message": "change"},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "change"


def test_steer_foreign_flow_forbidden_404(env: tuple[Any, Database]) -> None:
    """Чужой flow → 404 (не 403): существование скрыто, IDOR закрыт."""
    app, database = env
    flow_id = _create_flow(database, "alice", "a3")
    response = call(
        app, "POST", f"/flows/{flow_id}/steer",
        headers=auth("bob-token"), json={"message": "sneak"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "flow not found"}


def test_steer_invalid_token_rejected(env: tuple[Any, Database]) -> None:
    app, database = env
    flow_id = _create_flow(database, "alice", "a4")
    response = call(
        app, "POST", f"/flows/{flow_id}/steer",
        headers={"Authorization": "Bearer wrong-token"},
        json={"message": "sneak"},
    )
    assert response.status_code == 401
