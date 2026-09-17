from __future__ import annotations

import pathlib
import socket

import antigona.delivery as delivery_pkg


def test_no_prohibited_identifiers_in_delivery() -> None:
    delivery_dir = pathlib.Path(delivery_pkg.__file__).parent
    prohibited = {"klio", "hermes", "openclaw"}

    for py_file in delivery_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8").lower()
        for word in prohibited:
            assert word not in content, f"Prohibited clean-room word '{word}' found in {py_file}"


def test_mock_adapters_no_network(monkeypatch: object) -> None:
    # Ensure socket creation raises if mock adapters attempt network connections
    def fail_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("Socket connection attempted in mock adapter!")

    monkeypatch.setattr(socket, "socket", fail_socket)  # type: ignore[attr-defined]

    from antigona.config import Settings
    from antigona.delivery import ProgressEvent, get_adapter

    settings = Settings.from_env()
    event = ProgressEvent(
        task_id="t1",
        session_id="s1",
        correlation_id="c1",
        step_id=None,
        status="RUNNING",
        message="clean-room test",
    )

    for channel in ["telegram", "discord", "slack", "whatsapp", "signal", "email"]:
        adapter = get_adapter(channel, settings)
        adapter.deliver(event, f"key-{channel}")
