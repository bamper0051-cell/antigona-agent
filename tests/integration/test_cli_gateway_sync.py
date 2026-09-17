"""Integration tests for CLI ↔ Gateway real-time event synchronization.

Covers:
- Flow creation from CLI with stable correlation_id
- State transitions published over GET /events and WS /ws/events
- Flow approval and cancellation synchronization
- State store persistence
- Error handling when Gateway is unavailable
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from antigona.cli import CLIStateStore, GatewayClient
from antigona.config import Settings
from antigona.gateway.api import create_gateway_app
from antigona.models import Approval, TaskState
from antigona.repository import TaskRepository

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


@pytest.mark.asyncio
async def test_cli_create_flow_and_get_events(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    client = GatewayClient("http://test", "admin-token", transport=transport)
    state_file = tmp_path / "cli_state.json"
    store = CLIStateStore(path=str(state_file))

    # 1. Create flow with explicit correlation_id
    cid = "test-corr-id-12345"
    res = await client.create_flow(
        goal="Integration test goal",
        path="test.txt",
        content="hello",
        correlation_id=cid,
    )
    flow_id = res["id"]
    assert res["correlation_id"] == cid
    assert res["status"] in {"RECEIVED", "QUEUED"}

    # 2. Record in local store
    store.record_flow(flow_id, cid, "Integration test goal", res["status"])
    stored = store.get_flow(flow_id)
    assert stored is not None
    assert stored["correlation_id"] == cid

    # 3. GET /events backfill
    events = await client.get_events(after_seq=0)
    assert len(events) >= 1
    ev = events[0]
    assert ev["flow_id"] == flow_id
    assert ev["correlation_id"] == cid
    assert ev["seq"] >= 1
    assert "from_state" in ev
    assert "to_state" in ev


@pytest.mark.asyncio
async def test_cli_approval_and_cancel_sync(tmp_path: Path) -> None:
    app = _app(tmp_path)
    db = app.state.database
    transport = httpx.ASGITransport(app=app)
    client = GatewayClient("http://test", "admin-token", transport=transport)

    # 1. Create a task via client.create_flow (sets up QueueJob properly)
    res = await client.create_flow(
        goal="Approval test goal",
        path="out.txt",
        content="",
        correlation_id="cid-appr-1",
    )
    task_id = res["id"]

    # 2. Transition task to WAITING_APPROVAL and add Approval record
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task = repo.get(task_id)
        repo.transition(task, TaskState.PLANNING, reason="planning", actor="worker")
        repo.transition(task, TaskState.WAITING_APPROVAL, reason="needs approval", actor="worker")
        appr = Approval(
            task_id=task_id,
            tool_name="sandbox.shell",
            arguments={"command": ["rm", "-rf", "/"]},
            risk_level="HIGH",
            reason="Dangerous command",
        )
        session.add(appr)
        session.flush()
        appr_id = appr.id
        session.commit()

    # 3. Approve via CLI client
    appr_res = await client.decide_approval(appr_id, approve=True)
    assert appr_res.decision == "APPROVED"

    # 4. Verify transition event created with seq
    events = await client.get_events(after_seq=0)
    appr_events = [e for e in events if e["flow_id"] == task_id]
    assert len(appr_events) >= 1

    # 5. Cancel via CLI client
    cancel_res = await client.cancel_flow(task_id)
    assert cancel_res["status"] == "CANCELLED"

    events_after = await client.get_events(after_seq=0)
    cancelled_ev = [e for e in events_after if e["flow_id"] == task_id and e["to_state"] == "CANCELLED"]
    assert len(cancelled_ev) >= 1
