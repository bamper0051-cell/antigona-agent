"""Tests: send_file tool outbound delivery + risk gate."""

from __future__ import annotations

import json
import os

import pytest

from antigona.worker.hitl import RiskLevel, evaluate_risk

# ── risk gate ─────────────────────────────────────────────────────────────

def test_send_file_normal_is_medium():
    lvl, reason = evaluate_risk("send_file", {"path": "/tmp/report.txt"})
    assert lvl == RiskLevel.MEDIUM
    assert "outbound" in reason


def test_send_file_secret_is_high():
    for ext in (".env", ".pem", ".key"):
        lvl, _ = evaluate_risk("send_file", {"path": f"/tmp/secret{ext}"})
        assert lvl == RiskLevel.HIGH


# ── tool dispatch (mock) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_file_dispatches_normal(tmp_path):
    os.environ["ANTIGONA_DELIVERY_MOCK"] = "1"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID"] = "123"
    from antigona.tools.registry import ToolRegistry, register_builtins
    reg = ToolRegistry(); register_builtins(reg)
    f = tmp_path / "r.txt"
    f.write_text("hello")
    r = await reg.dispatch("send_file", path=str(f))
    assert '"success": true' in r
    assert '"mock": true' in r


@pytest.mark.asyncio
async def test_send_file_blocks_secret(tmp_path, monkeypatch):
    os.environ["ANTIGONA_DELIVERY_MOCK"] = "1"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID"] = "123"
    from antigona.delivery.adapter import TelegramAdapter
    from antigona.tools.registry import ToolRegistry, register_builtins

    sends = 0

    def track_send(*args, **kwargs):
        nonlocal sends
        sends += 1
        raise AssertionError("secret denial must not reach delivery")

    monkeypatch.setattr(TelegramAdapter, "send_file", track_send)
    reg = ToolRegistry(); register_builtins(reg)
    f = tmp_path / "secret.env"
    secret_value = "K=1"
    f.write_text(secret_value)

    response = json.loads(await reg.dispatch("send_file", path=str(f)))

    assert response == {
        "error": "HIGH-risk action requires approval grant before execution.",
        "requires_approval": True,
        "formatted_message": "",
    }
    assert secret_value not in json.dumps(response)
    assert sends == 0


@pytest.mark.asyncio
async def test_send_file_missing():
    from antigona.tools.registry import ToolRegistry, register_builtins
    reg = ToolRegistry(); register_builtins(reg)
    r = await reg.dispatch("send_file", path="/nope/x.txt")
    assert "not found" in r
