from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from antigona.config import Settings
from antigona.database import Database
from antigona.gateway.api import create_gateway_app


def _auth(token: str = "gateway-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _settings(tmp_path: Path, database_url: str, monkeypatch) -> Settings:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ANTIGONA_DATABASE_URL", database_url)
    return Settings(
        database_url=database_url,
        workspace=workspace,
        dev_tokens={"gateway-token": "owner-1"},
        sandbox_backend="inprocess",
        test_mode=True,
    )


def test_task_create_self_heals_when_verifier_criteria_table_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """A missing private table is initialized before criteria are written.

    The gateway starts with a normal schema init, then the verifier-only table is
    dropped to simulate schema drift. Submission must recreate it, persist criteria,
    and queue normally; first-boot ordering must not become an operational outage.
    """
    database_url = f"sqlite:///{tmp_path / 'db_missing_criteria.sqlite'}"
    settings = _settings(tmp_path, database_url, monkeypatch)
    gateway_app = create_gateway_app(settings)

    with TestClient(gateway_app, raise_server_exceptions=False) as gateway:
        # simulate the criteria table being absent at write time, regardless of the
        # shared init creating it on startup.
        with Database(database_url).engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS verifier_criteria")

        resp = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "fail-closed-missing-1"},
            json={"goal": "write a file", "path": "out.txt", "content": "hello"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "QUEUED"


def test_task_create_is_cancelled_when_criteria_write_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """A criteria write failure creates an audited terminal record without enqueue."""
    from antigona.verifier.criteria import VerifierCriteriaStore

    database_url = f"sqlite:///{tmp_path / 'db_write_raises.sqlite'}"
    settings = _settings(tmp_path, database_url, monkeypatch)
    gateway_app = create_gateway_app(settings)

    def _boom(self, task_id: str, criteria: str) -> None:
        raise RuntimeError("forced criteria write failure")

    monkeypatch.setattr(VerifierCriteriaStore, "put", _boom)

    with TestClient(gateway_app, raise_server_exceptions=False) as gateway:
        resp = gateway.post(
            "/flows",
            headers={**_auth(), "Idempotency-Key": "fail-closed-raise-1"},
            json={"goal": "write a file", "path": "out.txt", "content": "hello"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "CANCELLED"
        assert body["transitions"][-1]["reason"] == (
            "verifier criteria unavailable; task not queued"
        )
