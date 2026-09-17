import os

import pytest

from antigona.channels.base import FileAdapter
from antigona.transport.telegram import acquire_pid_lock


def test_telegram_transport_pid_lock_windows_nofollow(tmp_path, monkeypatch):
    pid_file = tmp_path / "test.pid"
    # Simulate environment where O_NOFOLLOW is absent (e.g. Windows)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    # Should not raise AttributeError: module 'os' has no attribute 'O_NOFOLLOW'
    lock = acquire_pid_lock(str(pid_file))
    try:
        assert lock is not None
    finally:
        if lock is not None:
            lock.close()


@pytest.mark.asyncio
async def test_file_adapter_connect_windows_nofollow(tmp_path, monkeypatch):
    log_file = tmp_path / "output.log"
    # Simulate environment where O_NOFOLLOW is absent (e.g. Windows)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    adapter = FileAdapter(str(log_file))
    # Should not raise AttributeError: module 'os' has no attribute 'O_NOFOLLOW'
    connected = await adapter.connect()
    try:
        assert connected is True
        assert adapter.connected is True
    finally:
        await adapter.disconnect()
