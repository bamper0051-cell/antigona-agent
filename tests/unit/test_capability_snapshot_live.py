"""Unit tests verifying live capability registry probing in ContextBuilder prompt snapshots."""

from __future__ import annotations

import pytest

from antigona.context.builder import ContextBuilder
from antigona.tools.capability_registry import get_capability_registry


def test_capability_snapshot_live_probe_available() -> None:
    """Live probe runs before prompt snapshot: working tools report AVAILABLE, not UNTESTED."""
    # Reset or retrieve registry singleton
    reg = get_capability_registry()

    builder = ContextBuilder()
    messages = builder.build()

    assert len(messages) >= 1
    system_content = messages[0]["content"]

    # Verify inventory header is present
    assert "CAPABILITY INVENTORY" in system_content

    # Verify live-probed capabilities report AVAILABLE
    assert "workspace.write: AVAILABLE" in system_content
    assert "workspace.read: AVAILABLE" in system_content
    assert "workspace.list: AVAILABLE" in system_content
    assert "system.time: AVAILABLE" in system_content
    assert "sandbox.shell: AVAILABLE" in system_content

    # Verify no probe-able working tool is left as UNTESTED
    for cap_id in ("workspace.write", "workspace.read", "workspace.list", "system.time", "sandbox.shell"):
        assert f"{cap_id}: UNTESTED" not in system_content
        cap = reg.get(cap_id)
        assert cap is not None
        assert cap.last_probe_time is not None


def test_capability_snapshot_preserves_not_implemented_and_none() -> None:
    """Non-implemented or unconfigured capabilities keep their accurate status."""
    builder = ContextBuilder()
    messages = builder.build()
    system_content = messages[0]["content"]

    assert "plugins: NONE" in system_content
    reg = get_capability_registry()
    # Browser truthfully reports UNTESTED when Playwright is installed but no
    # browser probe is executed; otherwise the dependency is unavailable.
    browser = reg.get("browser")
    assert browser is not None
    from importlib.util import find_spec
    expected = "browser: UNTESTED" if find_spec("playwright") is not None else "browser: NOT_IMPLEMENTED"
    assert expected in system_content


@pytest.mark.asyncio
async def test_capability_snapshot_live_inside_event_loop() -> None:
    """ContextBuilder.build() successfully probes even when called inside a running asyncio loop."""
    builder = ContextBuilder()
    messages = builder.build()
    system_content = messages[0]["content"]

    assert "workspace.write: AVAILABLE" in system_content
    assert "system.time: AVAILABLE" in system_content
