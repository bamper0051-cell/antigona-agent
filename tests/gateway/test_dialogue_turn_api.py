from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from antigona.config import Settings
from antigona.gateway.api import create_gateway_app

TOKENS = {"alice-token": "alice"}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        f"sqlite:///{tmp_path/'db.sqlite'}",
        tmp_path / "workspace",
        TOKENS,
        "inprocess",
        test_mode=True,
    )


@pytest.fixture()
def api_client(tmp_path: Path) -> TestClient:
    # Stage 1: the ONLY /api/v1/dialogue/turn endpoint lives in the canonical
    # Gateway (antigona.gateway). The legacy antigona.api.server app no longer
    # exposes it and must not construct its own DialogueEngine.
    app = create_gateway_app(make_settings(tmp_path))
    client = TestClient(app)
    client.__enter__()
    return client


def _auth(token: str = "alice-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_dialogue_turn_endpoint_request_response(api_client: TestClient) -> None:
    req_data = {
        "text": "Привет! Как дела?",
        "session_id": "test-session-123",
        "channel": "cli",
        "user_id": "user-456",
    }
    response = api_client.post("/api/v1/dialogue/turn", json=req_data, headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data
    assert data["session_id"] == "test-session-123"
    # Verified semantics: plain conversation is NOT verified.
    assert data["verified"] is None
    assert data["response_verified"] is False
    assert data["task_verified"] is False
    # Canonical brain routing result present
    assert data["response_type"] in {"conversation", "clarification", "error"}


def test_dialogue_turn_requires_auth(api_client: TestClient) -> None:
    """Turn API — owner-scoped endpoint: без Bearer-токена → 401."""
    response = api_client.post(
        "/api/v1/dialogue/turn",
        json={"text": "Кто я?", "session_id": "session-default"},
    )
    assert response.status_code == 401


def test_dialogue_turn_pydantic_defaults(api_client: TestClient) -> None:
    req_data = {
        "text": "Кто я?",
        "session_id": "session-default",
    }
    response = api_client.post("/api/v1/dialogue/turn", json=req_data, headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data
    assert data["reply"].strip() != ""
    # Ответ «Кто я?» — conversation identity, не clarification и не task.
    assert data["response_type"] == "conversation"
    assert "Владелец" in data["reply"]


def test_dialogue_turn_response_contract_has_routing_fields(api_client: TestClient) -> None:
    req_data = {
        "text": "Создай файл test.txt",
        "session_id": "session-task",
        "channel": "cli",
        "user_id": "user-task",
    }
    response = api_client.post("/api/v1/dialogue/turn", json=req_data, headers=_auth())
    assert response.status_code == 200
    data = response.json()
    # Thin-client contract: response_type + optional flow_id so CLI/Telegram do
    # not need a local IntentRouter to decide what to render.
    assert data["response_type"] in {"task_accepted", "conversation", "error", "clarification"}
    if data["response_type"] == "task_accepted":
        assert data["flow_id"] is not None
        # task_accepted — это приём, а не verified результат.
        assert data["task_verified"] is False
        assert data["verified"] is None or data["verified"] is False
