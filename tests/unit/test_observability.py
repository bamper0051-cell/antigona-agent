"""Tests for antigona.core.observability."""

from __future__ import annotations

from antigona.core.observability import (
    log_agent_turn,
    observability_enabled,
    start_run,
)


def test_disabled_by_default_noop(monkeypatch):
    monkeypatch.delenv("ANTIGONA_OBSERVABILITY", raising=False)
    monkeypatch.delenv("ANTIGONA_MLFLOW_TRACKING_URI", raising=False)
    assert observability_enabled() is False
    # no-ops must not raise and must report "not logged"
    assert log_agent_turn(session_id="s", prompt="p", reply="r") is False
    assert start_run() is None


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("ANTIGONA_OBSERVABILITY", "1")
    assert observability_enabled() is True


def test_log_returns_bool_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTIGONA_OBSERVABILITY", "1")
    monkeypatch.setenv("ANTIGONA_MLFLOW_TRACKING_URI", f"file:{tmp_path}/mlruns")
    # Should either log (True) or gracefully return False — never raise.
    result = log_agent_turn(
        session_id="s1",
        prompt="привет",
        reply="здравствуй",
        tokens_in=3,
        tokens_out=2,
        model="deepseek-v4-flash",
    )
    assert result in (True, False)
