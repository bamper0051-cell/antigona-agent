"""Wave B — ElevationAuthority: the one durable elevation + lockout store (CP-2)."""

from __future__ import annotations

import pytest

from antigona.security.elevation import ElevationAuthority


@pytest.fixture()
def auth(tmp_path) -> ElevationAuthority:
    return ElevationAuthority(
        db_path=tmp_path / "elev.db",
        max_attempts=3,
        lockout_seconds=100.0,
        session_ttl_seconds=50.0,
    )


# ── lockout ────────────────────────────────────────────────────────────────


def test_lockout_after_max_attempts_and_auto_clear(auth: ElevationAuthority) -> None:
    p = "owner:1"
    assert auth.is_locked_out(p, now=0.0) is False
    locked, remaining = auth.record_failure(p, now=1.0)
    assert (locked, remaining) == (False, 2)
    auth.record_failure(p, now=2.0)
    locked, remaining = auth.record_failure(p, now=3.0)
    assert locked is True and remaining == 0
    assert auth.is_locked_out(p, now=10.0) is True
    assert auth.remaining_lockout(p, now=10.0) == pytest.approx(93.0)
    # window elapses → auto-clears
    assert auth.is_locked_out(p, now=3.0 + 101.0) is False
    assert auth.failed_attempts(p) == 0


def test_record_success_clears_lockout(auth: ElevationAuthority) -> None:
    p = "owner:2"
    for i in range(3):
        auth.record_failure(p, now=float(i))
    assert auth.is_locked_out(p, now=5.0) is True
    auth.record_success(p)
    assert auth.is_locked_out(p, now=5.0) is False
    assert auth.failed_attempts(p) == 0


def test_lockout_is_durable_across_instances(tmp_path) -> None:
    db = tmp_path / "elev.db"
    a1 = ElevationAuthority(db_path=db, max_attempts=3, lockout_seconds=100.0)
    for i in range(3):
        a1.record_failure("owner:3", now=float(i))
    assert a1.is_locked_out("owner:3", now=5.0) is True
    # brand-new instance on the same file = a "restart"
    a2 = ElevationAuthority(db_path=db, max_attempts=3, lockout_seconds=100.0)
    assert a2.is_locked_out("owner:3", now=5.0) is True


# ── sessions ───────────────────────────────────────────────────────────────


def test_elevate_is_elevated_expiry(auth: ElevationAuthority) -> None:
    p = "cli:owner"
    assert auth.is_elevated(p, now=0.0) is False
    auth.elevate(p, now=0.0)
    assert auth.is_elevated(p, now=49.0) is True
    assert auth.is_elevated(p, now=51.0) is False  # ttl=50


def test_elevate_clears_lockout(auth: ElevationAuthority) -> None:
    p = "owner:4"
    auth.record_failure(p, now=0.0)
    auth.record_failure(p, now=1.0)
    auth.elevate(p, now=2.0)
    assert auth.failed_attempts(p) == 0


def test_revoke_and_revoke_all(auth: ElevationAuthority) -> None:
    auth.elevate("a", now=0.0)
    auth.elevate("b", now=0.0)
    assert auth.revoke("a") is True
    assert auth.revoke("a") is False
    assert auth.is_elevated("a", now=1.0) is False
    assert auth.is_elevated("b", now=1.0) is True
    assert auth.revoke_all() == 1


def test_sessions_are_durable_across_instances(tmp_path) -> None:
    db = tmp_path / "elev.db"
    ElevationAuthority(db_path=db, session_ttl_seconds=50.0).elevate("p", now=0.0)
    assert ElevationAuthority(db_path=db).is_elevated("p", now=10.0) is True


# ── fail-closed ────────────────────────────────────────────────────────────


def test_fail_closed_on_broken_db(tmp_path) -> None:
    a = ElevationAuthority(db_path=tmp_path / "x.db")
    a.db_path = str(tmp_path / "nonexistent_dir" / "\0bad")  # force a storage error
    assert a.is_locked_out("p") is True  # fail-closed → locked
    assert a.is_elevated("p") is False  # fail-closed → not elevated


# ── OwnerOverrideManager now uses it (durability regression) ────────────────


def test_owner_override_lockout_now_durable(tmp_path) -> None:
    from antigona.security.owner_override import OwnerOverrideManager

    m1 = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json")
    m1.set_pin("2468")
    for i in range(5):
        m1.verify_pin("0000", now=100.0 + i)
    assert m1.is_locked_out(now=110.0) is True

    m2 = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json")
    assert m2.is_locked_out(now=110.0) is True, "restart must not clear a durable lockout"
