"""P1 Phase 7: pin_gate unit tests (restored from pyc contract after rollback).

Wave B2 (CP-2): pin_gate's elevation + brute-force-lockout state moved to the
shared durable ``ElevationAuthority``. The security ASSERTIONS below are
unchanged; three tests switched their time injection from
``monkeypatch.setattr(pin_gate.time, "monotonic", ...)`` to the explicit ``now=``
parameter the delegated functions now accept (that monkeypatch no longer reaches
the lockout/session clock). Every such change is marked ``# B2:`` and recorded in
``evidence/control_plane_cleanup_20260828/03_auth_pin/WAVE_B2_EVIDENCE.md``.
"""
from __future__ import annotations

import asyncio

import pytest

from antigona.tools import pin_gate


@pytest.fixture(autouse=True)
def _isolate_elevation(tmp_path):
    """B2: isolate pin_gate's durable elevation store per test."""
    from antigona.security.elevation import ElevationAuthority

    pin_gate.set_elevation_authority(ElevationAuthority(db_path=tmp_path / "elev.db"))
    yield
    pin_gate.set_elevation_authority(None)


def test_verify_pin_correct_wrong_empty_and_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    assert pin_gate.verify_pin("1234") is True
    assert pin_gate.verify_pin("0000") is False
    assert pin_gate.verify_pin("") is False

    monkeypatch.delenv("ANTIGONA_PIN")
    assert pin_gate.is_pin_configured() is False
    assert pin_gate.verify_pin("1234") is False


def test_verify_pin_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """G3: constant-time comparison via hmac.compare_digest."""
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    import hmac

    calls: list[tuple[str, str]] = []

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(hmac, "compare_digest", _spy)
    assert pin_gate.verify_pin("1234") is True
    assert len(calls) == 1


def test_classify_action_including_unknown_fallback() -> None:
    """G6: unknown actions fall back to CRITICAL (fail-closed)."""
    assert pin_gate.classify_action("health_check") is pin_gate.RiskClass.SAFE
    assert pin_gate.classify_action("run_shell") is pin_gate.RiskClass.SENSITIVE
    assert pin_gate.classify_action("SECURITY_DISABLE") is pin_gate.RiskClass.CRITICAL
    assert pin_gate.classify_action("some_unknown_action") is pin_gate.RiskClass.CRITICAL
    assert pin_gate.classify_action(" danger ") is pin_gate.RiskClass.CRITICAL


def test_lockout_after_five_failures_and_reset_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_PIN", "1234")
    # B2: time via now= (was monkeypatch of pin_gate.time.monotonic). Assertions
    # unchanged: 5 wrong PINs → locked; lockout lifts one window after the last.
    t = 100.0
    chat_id = 42
    for i in range(5):
        success, _ = pin_gate.attempt_unlock(chat_id, "0000", now=t + i)
        assert success is False

    allowed, reason = pin_gate.check_unlock_possible(chat_id, now=t + 5)
    assert allowed is False
    assert "Слишком много" in reason

    allowed_after, _ = pin_gate.check_unlock_possible(chat_id, now=t + 5 + 901)
    assert allowed_after is True


def test_elevate_is_elevated_expiry_and_session_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_ELEVATION_TTL_SECONDS", "120")
    # B2: time via now= (was monkeypatch of pin_gate.time.monotonic). Assertions
    # unchanged except session_id: it is now a derived sha1 (was uuid4 hex) —
    # still opaque, non-empty, and round-tripped by get_session_info.
    t = 200.0
    session = pin_gate.elevate_session(955_111, 42, now=t)
    assert session.user_id == 42
    assert session.ttl_seconds == 120
    assert session.session_id  # opaque id present

    info = pin_gate.get_session_info(955_111, now=t)
    assert info is not None
    assert info["session_id"] == session.session_id
    assert info["remaining_seconds"] == 120

    assert pin_gate.is_elevated(955_111, now=t + 121) is False
    assert pin_gate.get_session_info(955_111, now=t + 121) is None


def test_mark_verified_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """G9: mark_verified uses the same TTL source as /unlock by default."""
    monkeypatch.setenv("ANTIGONA_OWNER_ELEVATION_TTL_SECONDS", "120")
    # B2: time via now= (was monkeypatch of pin_gate.time.monotonic). Assertion
    # unchanged: is_verified True within TTL, False after. (is_verified now reads
    # the same store as is_elevated — the separate _verified_sessions dict is
    # gone; no test asserted they must differ.)
    t = 300.0
    pin_gate.mark_verified(955_111, now=t)
    assert pin_gate.is_verified(955_111, now=t) is True
    assert pin_gate.is_verified(955_111, now=t + 121) is False


def test_confirm_valid_one_shot_wrong_user_payload_expiry_and_chat_scoping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_CONFIRM_TTL_SECONDS", "10")

    now = 200.0

    def _mono() -> float:
        return now

    monkeypatch.setattr(pin_gate.time, "monotonic", _mono)

    chat_id = 500
    user_id = 700
    pin_gate.elevate_session(chat_id, user_id)

    payload = {"action": "delete", "path": "/tmp/file"}
    token = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)

    assert pin_gate.confirm_action(chat_id, user_id, token) == payload
    assert pin_gate.confirm_action(chat_id, user_id, token) is None

    token_wrong_user = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)
    assert pin_gate.confirm_action(chat_id, user_id + 1, token_wrong_user) is None
    assert pin_gate.confirm_action(chat_id, user_id, token_wrong_user) == payload

    token_payload_a = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", {"payload": "a"})
    token_payload_b = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", {"payload": "b"})
    assert token_payload_a != token_payload_b
    assert pin_gate.confirm_action(chat_id, user_id, token_payload_a) is None
    assert pin_gate.confirm_action(chat_id, user_id, token_payload_b) == {"payload": "b"}

    token_expiry = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)
    now += 11
    assert pin_gate.confirm_action(chat_id, user_id, token_expiry) is None

    token_chat_scope = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)
    assert pin_gate.confirm_action(chat_id + 1, user_id, token_chat_scope) is None


def test_payload_user_id_is_ignored_for_confirmation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security review major: identity must come from the elevated session,
    never from untrusted payload content (user substitution attempt)."""
    monkeypatch.setenv("ANTIGONA_CONFIRM_TTL_SECONDS", "60")

    now = 500.0

    def _mono() -> float:
        return now

    monkeypatch.setattr(pin_gate.time, "monotonic", _mono)

    chat_id = 900
    owner_user_id = 42
    attacker_user_id = 999
    pin_gate.elevate_session(chat_id, owner_user_id)

    payload = {"user_id": attacker_user_id, "action": "delete", "path": "/tmp/x"}
    token = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)

    assert pin_gate.confirm_action(chat_id, attacker_user_id, token) is None
    assert pin_gate.confirm_action(chat_id, owner_user_id, token) == payload


@pytest.mark.anyio
async def test_concurrent_confirm_one_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """G10/G8: two concurrent confirms of the same token — exactly one wins."""
    monkeypatch.setenv("ANTIGONA_CONFIRM_TTL_SECONDS", "60")

    now = 1000.0

    def _mono() -> float:
        return now

    monkeypatch.setattr(pin_gate.time, "monotonic", _mono)

    chat_id = 777
    user_id = 888
    pin_gate.elevate_session(chat_id, user_id)

    payload = {"action": "delete_file", "path": "/tmp/file"}
    token = pin_gate.set_pending_confirmation(chat_id, "DELETE_FILE", payload)

    results = await asyncio.gather(
        asyncio.to_thread(pin_gate.confirm_action, chat_id, user_id, token),
        asyncio.to_thread(pin_gate.confirm_action, chat_id, user_id, token),
    )
    successes = [r for r in results if r is not None]
    assert len(successes) == 1
    assert successes[0] == payload


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
