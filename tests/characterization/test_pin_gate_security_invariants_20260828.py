"""CHARACTERIZATION — pin_gate elevation/lockout security invariants (Wave B2).

An independent record of the security properties `tools.pin_gate` must keep while
Wave B2 moves its elevation + brute-force-lockout state onto the shared
``ElevationAuthority`` (finding CP-2). The ASSERTIONS here are the invariants; the
*time-injection mechanism* is refactored during B2 (monotonic monkeypatch → an
explicit ``now=`` parameter) — see the ``# B2:`` notes and
``evidence/.../03_auth_pin/WAVE_B2_EVIDENCE.md`` for every changed expectation.

Confirmation state (``_pending_confirmations`` / ``confirm_*``) is out of scope —
that is CRITICAL-approval, not elevation, and is not touched by B2.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def pin_gate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("ANTIGONA_PIN", "1379")
    from antigona.tools import pin_gate as pg

    importlib.reload(pg)
    # B2: isolate the durable elevation store to tmp (added with the refactor;
    # a no-op before B2 when pin_gate keeps state in module globals).
    if hasattr(pg, "set_elevation_authority"):
        from antigona.security.elevation import ElevationAuthority

        pg.set_elevation_authority(ElevationAuthority(db_path=tmp_path / "elev.db"))
    yield pg
    if hasattr(pg, "set_elevation_authority"):
        pg.set_elevation_authority(None)
    importlib.reload(pg)


def _at(pin_gate, base: float, offset: float):
    """Time-inject helper. B2: prefer now= kwarg; fall back to monotonic patch."""
    return {"now": base + offset}


# ── INV-1: brute-force lockout after _MAX_ATTEMPTS wrong PINs ───────────────


def test_inv1_lockout_after_max_attempts(pin_gate) -> None:
    chat = 700
    base = 1_000.0
    for i in range(1, pin_gate._MAX_ATTEMPTS):
        ok, msg = pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, i))
        assert ok is False and "Осталось попыток" in msg
    ok, msg = pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, pin_gate._MAX_ATTEMPTS))
    assert ok is False
    allowed, _ = pin_gate.check_unlock_possible(chat, **_at(pin_gate, base, 10))
    assert allowed is False, "INV-1: chat is locked out after _MAX_ATTEMPTS failures"


def test_inv1b_correct_pin_refused_while_locked(pin_gate) -> None:
    chat = 701
    base = 2_000.0
    for i in range(pin_gate._MAX_ATTEMPTS):
        pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, i))
    ok, _ = pin_gate.attempt_unlock(chat, "1379", **_at(pin_gate, base, 10))
    assert ok is False, "INV-1b: the correct PIN must NOT unlock while locked out"


# ── INV-2: lockout auto-clears after the window ────────────────────────────


def test_inv2_lockout_clears_after_window(pin_gate) -> None:
    chat = 702
    base = 3_000.0
    for i in range(pin_gate._MAX_ATTEMPTS):
        pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, i))
    # still locked partway through the window
    mid = pin_gate._LOCKOUT_SECONDS / 2
    assert pin_gate.check_unlock_possible(chat, **_at(pin_gate, base, mid))[0] is False
    # lifts once the full window has elapsed from the LAST failed attempt
    after = pin_gate._MAX_ATTEMPTS + pin_gate._LOCKOUT_SECONDS + 1
    allowed, _ = pin_gate.check_unlock_possible(chat, **_at(pin_gate, base, after))
    assert allowed is True, "INV-2: lockout lifts after _LOCKOUT_SECONDS"
    ok, _ = pin_gate.attempt_unlock(chat, "1379", **_at(pin_gate, base, after + 1))
    assert ok is True


# ── INV-3: a successful unlock resets the failure counter ──────────────────


def test_inv3_success_resets_attempts(pin_gate) -> None:
    chat = 703
    base = 4_000.0
    for i in range(pin_gate._MAX_ATTEMPTS - 1):
        pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, i))
    ok, _ = pin_gate.attempt_unlock(chat, "1379", **_at(pin_gate, base, 10))
    assert ok is True
    # a fresh streak of wrong PINs must start from zero, not from 4
    for i in range(pin_gate._MAX_ATTEMPTS - 1):
        pin_gate.attempt_unlock(chat, "0000", **_at(pin_gate, base, 20 + i))
    allowed, _ = pin_gate.check_unlock_possible(chat, **_at(pin_gate, base, 40))
    assert allowed is True, "INV-3: success cleared the counter"


# ── INV-4: elevate_session lifecycle + TTL expiry ─────────────────────────


def test_inv4_elevate_then_expire(pin_gate) -> None:
    chat = 704
    base = 5_000.0
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 0)) is False
    pin_gate.elevate_session(chat, 42, **_at(pin_gate, base, 1))
    ttl = pin_gate._get_ttl()
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 1 + ttl - 1)) is True
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 1 + ttl + 1)) is False


# ── INV-5: mark_verified lifecycle + TTL expiry ───────────────────────────


def test_inv5_mark_verified_then_expire(pin_gate) -> None:
    chat = 705
    base = 6_000.0
    assert pin_gate.is_verified(chat, **_at(pin_gate, base, 0)) is False
    pin_gate.mark_verified(chat, ttl_seconds=120, **_at(pin_gate, base, 1))
    assert pin_gate.is_verified(chat, **_at(pin_gate, base, 100)) is True
    assert pin_gate.is_verified(chat, **_at(pin_gate, base, 200)) is False


# ── INV-6: lock_session revokes elevation ─────────────────────────────────


def test_inv6_lock_session_revokes(pin_gate) -> None:
    chat = 706
    base = 7_000.0
    pin_gate.elevate_session(chat, 42, **_at(pin_gate, base, 0))
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 1)) is True
    pin_gate.lock_session(chat)
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 2)) is False


# ── INV-7: verify_pin is fail-closed + constant-time SHA-256 ──────────────


def test_inv7_verify_pin_fail_closed_and_compare_digest(
    pin_gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert pin_gate.verify_pin("1379") is True
    assert pin_gate.verify_pin("0000") is False
    assert pin_gate.verify_pin("") is False
    import hmac

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or a == b)
    assert pin_gate.verify_pin("1379") is True
    assert len(calls) == 1
    monkeypatch.delenv("ANTIGONA_PIN")
    assert pin_gate.verify_pin("1379") is False


# ── INV-8: reset_all_sessions clears elevation + lockout ──────────────────


def test_inv8_reset_all_sessions(pin_gate) -> None:
    chat = 707
    base = 8_000.0
    pin_gate.elevate_session(chat, 42, **_at(pin_gate, base, 0))
    for i in range(pin_gate._MAX_ATTEMPTS):
        pin_gate.attempt_unlock(999, "0000", **_at(pin_gate, base, i))
    pin_gate.reset_all_sessions()
    assert pin_gate.is_elevated(chat, **_at(pin_gate, base, 1)) is False
    allowed, _ = pin_gate.check_unlock_possible(999, **_at(pin_gate, base, 2))
    assert allowed is True
