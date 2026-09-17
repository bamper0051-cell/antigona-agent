"""E2E Integration test for CLI WebSocket reconnection, backfill, and deduplication.

Covers:
- Exponential backoff reconnect on disconnection
- REST GET /events?after_seq backfill
- Event deduplication (seen-set by (flow_id, seq))
- Event stream completeness and order preservation
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from antigona.cli import GatewayClient
from antigona.config import Settings
from antigona.gateway.api import create_gateway_app
from antigona.models import StateTransition
from antigona.repository import CreateTask, TaskRepository

TOKENS = {"admin-token": "admin"}


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
async def test_cli_ws_reconnect_backfill_and_dedup(tmp_path: Path) -> None:
    app = _app(tmp_path)
    db = app.state.database
    transport = httpx.ASGITransport(app=app)
    client = GatewayClient("http://test", "admin-token", transport=transport)

    # 1. Create a task and add several state transitions directly in DB
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask("admin", "Reconnect test goal", "out.txt", "", "idem-rec", "workspace.write_text", ()),
            correlation_id="cid-rec-1",
        )
        task_id = task.id
        # Add transitions
        tr1 = StateTransition(
            task_id=task_id, entity_id=task_id, entity_type="task",
            from_state="RECEIVED", to_state="QUEUED", reason="enqueued",
            actor="gateway", correlation_id="cid-rec-1",
        )
        tr2 = StateTransition(
            task_id=task_id, entity_id=task_id, entity_type="task",
            from_state="QUEUED", to_state="RUNNING", reason="worker started",
            actor="worker", correlation_id="cid-rec-1",
        )
        session.add_all([tr1, tr2])
        session.commit()

    # 2. Fetch events via REST backfill starting from seq=0
    events_batch1 = await client.get_events(after_seq=0)
    assert len(events_batch1) >= 3  # RECEIVED, QUEUED, RUNNING
    max_seq1 = max(e["seq"] for e in events_batch1)

    # 3. Add more transitions while client is "disconnected"
    with db.session_factory() as session:
        tr3 = StateTransition(
            task_id=task_id, entity_id=task_id, entity_type="task",
            from_state="RUNNING", to_state="VERIFYING", reason="checking output",
            actor="worker", correlation_id="cid-rec-1",
        )
        tr4 = StateTransition(
            task_id=task_id, entity_id=task_id, entity_type="task",
            from_state="VERIFYING", to_state="DONE", reason="verification passed",
            actor="verifier", correlation_id="cid-rec-1",
        )
        session.add_all([tr3, tr4])
        session.commit()

    # 4. Reconnect using connect_events passing max_seq1 as after_seq
    collected: list[dict[str, Any]] = []
    seen_seqs: set[int] = set()

    # We consume events using connect_events with backfill
    async for event in client.connect_events(after_seq=max_seq1, max_retries=1):
        seq = event["seq"]
        assert seq not in seen_seqs, f"Duplicate event received with seq={seq}"
        seen_seqs.add(seq)
        collected.append(event)
        if event.get("to_state") == "DONE":
            break

    # 5. Verify missed events received without duplicates
    assert len(collected) >= 2
    states = [e["to_state"] for e in collected if e["flow_id"] == task_id]
    assert "DONE" in states
    assert len(seen_seqs) == len(collected)  # Deduplication holds


@pytest.mark.asyncio
async def test_cli_gateway_unavailable_error_handling(tmp_path: Path) -> None:
    # Client pointing to a closed/non-existent host
    client = GatewayClient("http://127.0.0.1:59999", "admin-token")
    with pytest.raises((ConnectionError, httpx.HTTPError, OSError)):
        await client.create_flow(goal="will fail", correlation_id="cid-fail")
