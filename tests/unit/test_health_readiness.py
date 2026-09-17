"""Milestone 0 (P0 Security) — health/readiness tests (scope 4)."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from antigona.database import Database
from antigona.health.heartbeat import (
    HeartbeatReporter,
    _health_dir,
    read_status,
)
from antigona.verifier_service import create_verifier_app


def test_heartbeat_reporter_touch_marks_up(tmp_path, monkeypatch):
    _use_dev_project_root(monkeypatch, tmp_path)
    rep = HeartbeatReporter("worker", interval_seconds=60)
    rep.touch()
    status = read_status(window_seconds=60)
    assert status["worker"]["up"] is True
    assert status["worker"]["pid"] is not None


def test_read_status_stale_is_down(tmp_path, monkeypatch):
    _use_dev_project_root(monkeypatch, tmp_path)
    rep = HeartbeatReporter("delivery", interval_seconds=60)
    rep.touch()
    # Rewrite the heartbeat with an old timestamp.
    payload = {
        "service": "delivery",
        "pid": 1,
        "ts": time.time() - 1000,
        "status": "up",
    }
    (_health_dir() / "delivery.json").write_text(
        __import__("json").dumps(payload), encoding="utf-8"
    )
    status = read_status(window_seconds=60)
    assert status["delivery"]["up"] is False


def test_read_status_missing_is_down(tmp_path, monkeypatch):
    _use_dev_project_root(monkeypatch, tmp_path)
    status = read_status(window_seconds=60)
    assert "nope" not in status or status.get("nope", {}).get("up") is False


def test_verifier_health_and_readyz(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    db = Database(url)
    db.create_all()

    class _MockJudge:
        model_name = "mock-verifier"

        def evaluate(self, *_a, **_k):  # type: ignore[no-untyped-def]
            from antigona.verifier.judge import ProviderResult

            return ProviderResult(
                approved=True, reason="mock pass", actual_model="mock-verifier"
            )

    app = create_verifier_app(url, credential="secret", judge=_MockJudge())  # type: ignore[arg-type]
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["service"] == "verifier"

        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
        assert r.json()["database"] == "ok"


def _use_dev_project_root(monkeypatch, tmp_path):
    """Point the governed health resolver at a temp dev code root."""
    monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
    monkeypatch.delenv("ANTIGONA_HEALTH_DIR", raising=False)
    monkeypatch.setattr("antigona.core.paths.project_root", lambda: tmp_path)
