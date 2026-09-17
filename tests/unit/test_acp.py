"""Tests for antigona.core.acp."""

from __future__ import annotations

import asyncio

import pytest

from antigona.core.acp import ACPClient, ACPRegistry, acp_available


def test_acp_available_flag():
    assert isinstance(acp_available(), bool)


def test_registry_add_remove():
    r = ACPRegistry()
    r.add("codex", "http://127.0.0.1:8000")
    r.add("claude", "http://127.0.0.1:8001")
    assert r.names() == ["claude", "codex"]
    assert r.remove("codex") is True
    assert r.remove("nope") is False
    assert r.names() == ["claude"]


def test_registry_roundtrip():
    r = ACPRegistry()
    r.add("a", "http://x")
    r2 = ACPRegistry.from_dict(r.to_dict())
    assert r2.to_dict() == r.to_dict()


@pytest.mark.asyncio
async def test_unreachable_server_safe():
    # connecting to a dead server must not hang/crash the process
    client = ACPClient("http://127.0.0.1:1")
    try:
        await asyncio.wait_for(client.connect(), timeout=5)
    except Exception:
        pass
    else:
        # if connect succeeded (unlikely against :1), cleanup
        await client.aclose()


def test_acp_available_consistent_with_sdk():
    import importlib.util

    if importlib.util.find_spec("acp_sdk") is not None:
        assert acp_available() is True
