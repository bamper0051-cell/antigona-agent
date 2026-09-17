import os
import tempfile
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from antigona.config import Settings
from antigona.cron import CronScheduler
from antigona.gateway.api import create_gateway_app
from antigona.models import utcnow

OWNER_TOKEN = "test-owner-token"
OWNER_ID = "test-owner"
OTHER_TOKEN = "other-token"
OTHER_ID = "other-owner"


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    # Use a temp file for SQLite to avoid in-memory-per-connection issues
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    settings = Settings(
        database_url=f"sqlite:///{tmp_db.name}",
        workspace=Path("."),
        dev_tokens={OWNER_TOKEN: OWNER_ID, OTHER_TOKEN: OTHER_ID},
    )
    app = create_gateway_app(settings)
    with TestClient(app) as tc:
        yield tc
    try:
        os.unlink(tmp_db.name)
    except PermissionError:
        pass  # Windows: SQLite may still hold the file — best-effort


def _auth_headers(token: str = OWNER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_create_schedule_via_gateway(client: TestClient) -> None:
    resp = client.post(
        "/schedules",
        json={
            "name": "test_schedule",
            "cron_expression": "*/5 * * * *",
            "goal": "Test goal",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test_schedule"
    assert data["cron_expression"] == "*/5 * * * *"
    assert data["target_path"] == "workspace"
    assert data["enabled"] is True
    assert data["cancelled"] is False
    assert data["owner_id"] == OWNER_ID
    assert "next_run_at" in data
    assert "id" in data


def test_create_schedule_invalid_cron(client: TestClient) -> None:
    resp = client.post(
        "/schedules",
        json={
            "name": "bad",
            "cron_expression": "not-a-cron",
            "goal": "g",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_list_schedules_via_gateway(client: TestClient) -> None:
    client.post(
        "/schedules",
        json={"name": "a", "cron_expression": "* * * * *", "goal": "ga"},
        headers=_auth_headers(),
    )
    client.post(
        "/schedules",
        json={"name": "b", "cron_expression": "* * * * *", "goal": "gb"},
        headers=_auth_headers(),
    )
    resp = client.get("/schedules", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_cancel_schedule_via_gateway(client: TestClient) -> None:
    create_resp = client.post(
        "/schedules",
        json={"name": "c", "cron_expression": "* * * * *", "goal": "gc"},
        headers=_auth_headers(),
    )
    sched_id = create_resp.json()["id"]

    cancel_resp = client.post(
        f"/schedules/{sched_id}/cancel",
        headers=_auth_headers(),
    )
    assert cancel_resp.status_code == 200
    data = cancel_resp.json()
    assert data["cancelled"] is True
    assert data["enabled"] is False

    # Cancelled schedule should not produce tasks via tick
    with client.app.state.database.session_factory() as session:
        sched = CronScheduler(session).get_schedule(sched_id, owner_id=OWNER_ID)
        sched.next_run_at = utcnow() - timedelta(minutes=5)
        session.commit()
        tasks = CronScheduler(session).tick()
    assert len(tasks) == 0


def test_owner_isolation_gateway(client: TestClient) -> None:
    create_resp = client.post(
        "/schedules",
        json={"name": "d", "cron_expression": "* * * * *", "goal": "gd"},
        headers=_auth_headers(token=OWNER_TOKEN),
    )
    sched_id = create_resp.json()["id"]

    # Other owner cannot read
    resp = client.get(f"/schedules/{sched_id}", headers=_auth_headers(token=OTHER_TOKEN))
    assert resp.status_code == 404

    # Other owner cannot cancel
    resp = client.post(f"/schedules/{sched_id}/cancel", headers=_auth_headers(token=OTHER_TOKEN))
    assert resp.status_code == 404

    # Other owner's list does not include this schedule
    resp = client.get("/schedules", headers=_auth_headers(token=OTHER_TOKEN))
    assert len(resp.json()) == 0


def test_no_auth_returns_401(client: TestClient) -> None:
    resp = client.post("/schedules", json={"name": "x", "cron_expression": "* * * * *", "goal": "g"})
    assert resp.status_code == 401

    resp = client.get("/schedules")
    assert resp.status_code == 401

    resp = client.post("/schedules/some-id/cancel")
    assert resp.status_code == 401


def test_tick_endpoint(client: TestClient) -> None:
    create_resp = client.post(
        "/schedules",
        json={"name": "e", "cron_expression": "* * * * *", "goal": "ge"},
        headers=_auth_headers(),
    )
    sched_id = create_resp.json()["id"]

    with client.app.state.database.session_factory() as session:
        sched = CronScheduler(session).get_schedule(sched_id, owner_id=OWNER_ID)
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

    resp = client.post("/schedules/tick", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticked"] is True
    assert data["tasks_created"] >= 1
    assert data["errors"] == 0


def test_tick_endpoint_reports_errors_for_malformed_schedule(client: TestClient) -> None:
    good_resp = client.post(
        "/schedules",
        json={"name": "good", "cron_expression": "* * * * *", "goal": "g-good"},
        headers=_auth_headers(),
    )
    good_id = good_resp.json()["id"]

    bad_resp = client.post(
        "/schedules",
        json={"name": "bad", "cron_expression": "* * * * *", "goal": "g-bad"},
        headers=_auth_headers(),
    )
    bad_id = bad_resp.json()["id"]

    with client.app.state.database.session_factory() as session:
        scheduler = CronScheduler(session)
        good_sched = scheduler.get_schedule(good_id, owner_id=OWNER_ID)
        good_sched.next_run_at = utcnow() - timedelta(minutes=1)
        bad_sched = scheduler.get_schedule(bad_id, owner_id=OWNER_ID)
        # Malformed directly in DB: bypasses the API-level default/validation.
        bad_sched.target_path = "."
        bad_sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

    resp = client.post("/schedules/tick", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticked"] is True
    # Batch resilience: the good schedule still produces a task despite the bad one.
    assert data["tasks_created"] == 1
    assert data["errors"] == 1

    with client.app.state.database.session_factory() as session:
        scheduler = CronScheduler(session)
        bad_events = scheduler.get_events(bad_id, owner_id=OWNER_ID)
        good_events = scheduler.get_events(good_id, owner_id=OWNER_ID)
    assert any(e.event_type == "errored" for e in bad_events)
    assert any(e.event_type == "ticked" for e in good_events)


def test_cron_jobs_endpoint(client: TestClient) -> None:
    create_resp = client.post(
        "/schedules",
        json={"name": "f", "cron_expression": "* * * * *", "goal": "gf"},
        headers=_auth_headers(),
    )
    sched_id = create_resp.json()["id"]

    with client.app.state.database.session_factory() as session:
        sched = CronScheduler(session).get_schedule(sched_id, owner_id=OWNER_ID)
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()
        CronScheduler(session).tick(correlation_id="test")

    resp = client.get(f"/schedules/{sched_id}/jobs", headers=_auth_headers())
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) >= 1
    assert jobs[0]["goal"] == "gf"


def test_get_schedule_by_id(client: TestClient) -> None:
    create_resp = client.post(
        "/schedules",
        json={"name": "g", "cron_expression": "0 0 * * *", "goal": "gg"},
        headers=_auth_headers(),
    )
    sched_id = create_resp.json()["id"]

    resp = client.get(f"/schedules/{sched_id}", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sched_id
    assert data["name"] == "g"
    assert data["cron_expression"] == "0 0 * * *"


def test_get_schedule_not_found(client: TestClient) -> None:
    resp = client.get("/schedules/nonexistent", headers=_auth_headers())
    assert resp.status_code == 404
