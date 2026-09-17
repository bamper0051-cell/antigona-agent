"""Tests: worker can send files outbound to Telegram via send_file tool."""

from __future__ import annotations

import os

import pytest

from antigona.worker.agent_core import WorkerAgentCore


@pytest.fixture(autouse=True)
def _env():
    os.environ["ANTIGONA_DELIVERY_MOCK"] = "1"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN"] = "test-token"
    os.environ["ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID"] = "12345"
    yield
    os.environ.pop("ANTIGONA_DELIVERY_MOCK", None)


def test_worker_registers_send_file_tool():
    import inspect
    src = inspect.getsource(WorkerAgentCore._register_tools)
    assert "send_file" in src
    assert "workspace.write_text" in src


def test_worker_has_tool_send_file():
    assert hasattr(WorkerAgentCore, "_tool_send_file")


def test_worker_send_file_delivers_normal(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("hello")
    core = WorkerAgentCore.__new__(WorkerAgentCore)
    core.untrusted_context = False
    core._current_task = None
    core._current_step = None
    res = core._tool_send_file({"path": str(f)})
    assert res.get("ok") is True
    assert res.get("delivered", {}).get("mock") is True


def test_worker_send_file_blocks_secret(tmp_path):
    f = tmp_path / "secret.env"
    f.write_text("K=1")
    core = WorkerAgentCore.__new__(WorkerAgentCore)
    core.untrusted_context = False
    core._current_task = None
    core._current_step = None
    res = core._tool_send_file({"path": str(f)})
    assert res.get("ok") is False
    assert res.get("blocked") is True
