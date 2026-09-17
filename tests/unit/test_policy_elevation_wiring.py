"""Wave B3 — PolicyEngine consults the canonical ElevationAuthority (CP-7).

RED before B3: PolicyEngine built without an OwnerOverrideManager (the canonical
`unified_executor` path) never saw any elevation — the owner-override branch was
inert. GREEN: with no injected manager it reads the shared ElevationAuthority
under the canonical `principal_for(channel, user_id, session_id)` key, so an
unlock (e.g. OwnerOverrideManager.verify_and_elevate) is now visible to the
policy check.
"""

from __future__ import annotations

import pytest

from antigona.policy.engine import PolicyEngine
from antigona.security.elevation import ElevationAuthority, principal_for

_CH, _UID, _SID = "cli", "owner", "s-b3"


@pytest.fixture()
def elevation(tmp_path) -> ElevationAuthority:
    return ElevationAuthority(db_path=tmp_path / "elev.db")


async def _verdict(pe: PolicyEngine, action: str = "git"):
    return await pe.check(
        action=action,
        params={"command": "git status"},
        context={"channel": _CH, "user_id": _UID, "session_id": _SID},
    )


@pytest.mark.asyncio
async def test_medium_action_requires_approval_when_not_elevated(elevation) -> None:
    pe = PolicyEngine(elevation=elevation)  # require_approval defaults True
    v = await _verdict(pe)
    assert v["risk_level"] == "MEDIUM", v  # guard: we are exercising the SENSITIVE branch
    assert v["allowed"] is True
    assert v["requires_approval"] is True, "RED baseline: not elevated → approval required"


@pytest.mark.asyncio
async def test_medium_action_skips_approval_when_elevated_in_canonical_store(elevation) -> None:
    pe = PolicyEngine(elevation=elevation)
    elevation.elevate(principal_for(_CH, _UID, _SID))
    v = await _verdict(pe)
    assert v["allowed"] is True
    assert v["requires_approval"] is False, "CP-7: canonical elevation must lift the approval req"
    assert "Owner Override" in v["reason"]


@pytest.mark.asyncio
async def test_elevation_is_triple_scoped(elevation) -> None:
    pe = PolicyEngine(elevation=elevation)
    elevation.elevate(principal_for(_CH, _UID, _SID))
    # different session_id → not elevated
    other = await pe.check(
        action="git",
        params={"command": "git status"},
        context={"channel": _CH, "user_id": _UID, "session_id": "OTHER"},
    )
    assert other["requires_approval"] is True


@pytest.mark.asyncio
async def test_critical_still_2step_even_when_elevated(elevation) -> None:
    pe = PolicyEngine(elevation=elevation)
    elevation.elevate(principal_for(_CH, _UID, _SID))
    v = await pe.check(
        action="run_shell",
        params={"command": "rm -rf /tmp/x"},
        context={"channel": _CH, "user_id": _UID, "session_id": _SID},
    )
    assert v["allowed"] is False
    assert v["requires_2step_confirmation"] is True, "elevation must NOT bypass CRITICAL"


@pytest.mark.asyncio
async def test_owner_override_manager_and_policy_engine_agree(tmp_path) -> None:
    """An unlock via OwnerOverrideManager.verify_and_elevate is visible to a
    PolicyEngine check for the same (channel, user_id, session_id)."""
    from antigona.security.owner_override import OwnerOverrideManager

    elev = ElevationAuthority(db_path=tmp_path / "elev.db")
    mgr = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json", elevation=elev)
    mgr.set_pin("2468")
    ok, _ = mgr.verify_and_elevate(_CH, _UID, _SID, "2468")  # wall clock, like the check
    assert ok is True

    pe = PolicyEngine(elevation=elev)
    v = await _verdict(pe)
    assert v["requires_approval"] is False


@pytest.mark.asyncio
async def test_injected_owner_override_still_wins(tmp_path) -> None:
    """Back-compat: an explicitly injected OwnerOverrideManager is still the
    source (the legacy action_executor path)."""
    from antigona.security.owner_override import OwnerOverrideManager

    elev = ElevationAuthority(db_path=tmp_path / "elev.db")
    mgr = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json", elevation=elev)
    pe = PolicyEngine(owner_override=mgr)
    mgr.elevate_session(_CH, _UID, _SID)  # wall clock, like the check
    v = await _verdict(pe)
    assert v["requires_approval"] is False
