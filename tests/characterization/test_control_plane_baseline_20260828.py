"""CHARACTERIZATION baseline — Control-Plane Sanitation campaign, Block 4.

These tests PIN THE CURRENT BEHAVIOUR of the auth / PIN / approval control plane
on canonical strata ``campaign/canonical-20260828``. They are *characterization*
tests (Michael Feathers sense): they assert what the code does today, defects
included, so a later strangler-cleanup wave can see exactly what it changes.

A failing test here after a cleanup wave is EXPECTED and means "document the
behaviour change" — not "revert". Do not treat these as correctness specs.

Findings referenced: CP-1 (approval leaks), CP-2 (PIN state fragmentation),
CP-5/CP-7 (elevation store mismatch), CP-8 (CRITICAL 2-step is Telegram-only).
See docs/control_plane/01_CONTROL_PLANE_INVENTORY.md + 02_CLI_TELEGRAM_PARITY_TRACE.md.
"""

from __future__ import annotations

import importlib

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# 1. tools/pin_gate.py — wrong PIN x1 / x3 / x5, lockout, and RESTART
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def pin_gate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("ANTIGONA_PIN", "1379")
    from antigona.tools import pin_gate as pg

    importlib.reload(pg)
    # B2: pin_gate's elevation/lockout state is now the durable ElevationAuthority
    # — isolate it per test (a bare importlib.reload no longer resets it).
    from antigona.security.elevation import ElevationAuthority

    pg.set_elevation_authority(ElevationAuthority(db_path=tmp_path / "elev.db"))
    yield pg
    pg.set_elevation_authority(None)
    importlib.reload(pg)


def test_pin_gate_wrong_pin_x1_x3_x5_then_lockout(pin_gate) -> None:
    chat = 4242
    t = 1_000.0
    results: list[tuple[int, bool, str]] = []
    for attempt_no in range(1, 6):
        ok, msg = pin_gate.attempt_unlock(chat, "0000", now=t + attempt_no)
        results.append((attempt_no, ok, msg))

    # x1..x4: rejected with a decreasing "remaining attempts" counter
    assert results[0][1] is False and "Осталось попыток: 4" in results[0][2]
    assert results[2][1] is False and "Осталось попыток: 2" in results[2][2]
    # x5: rejected AND locked out
    assert results[4][1] is False
    assert "заблокирован" in results[4][2].lower() or "15 мин" in results[4][2]

    # further attempts are refused up-front (lockout gate), even with the RIGHT pin
    ok, msg = pin_gate.attempt_unlock(chat, "1379", now=t + 10)
    assert ok is False, "correct PIN must be refused while locked out"
    assert "15 минут" in msg or "заблокирован" in msg.lower()

    # B2: lockout now lives in the durable ElevationAuthority (was a module dict)
    assert pin_gate._get_elevation().is_locked_out(pin_gate._principal(chat), now=t + 10)


def test_pin_gate_lockout_SURVIVES_restart(pin_gate, tmp_path) -> None:
    """BEHAVIOUR DELTA — Wave B2 (CP-2). Was ``..._does_NOT_survive_restart``.

    pin_gate's lockout is now the shared durable ElevationAuthority, so a fresh
    process (new authority instance on the same DB) KEEPS the brute-force
    lockout. Same intent, flipped expectation.
    """
    from antigona.security.elevation import ElevationAuthority

    chat = 4243
    t = 2_000.0
    for i in range(5):
        pin_gate.attempt_unlock(chat, "0000", now=t + i)
    assert pin_gate.check_unlock_possible(chat, now=t + 10)[0] is False

    # "restart" = reload the module AND rebind a NEW authority to the SAME db file
    db = pin_gate._get_elevation().db_path
    importlib.reload(pin_gate)
    pin_gate.set_elevation_authority(ElevationAuthority(db_path=db))

    allowed_after, _ = pin_gate.check_unlock_possible(chat, now=t + 10)
    assert allowed_after is False, "durable lockout must survive restart"
    assert pin_gate.attempt_unlock(chat, "1379", now=t + 11)[0] is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. pin_gate: is_elevated / is_verified — ONE store after Wave B2 (CP-2)
# ─────────────────────────────────────────────────────────────────────────────


def test_is_elevated_and_is_verified_share_one_store(pin_gate) -> None:
    """BEHAVIOUR DELTA — Wave B2 (CP-2). Was ``..._are_separate_stores``.

    The ``_verified_sessions`` dict is gone: ``is_verified`` now reads the same
    ElevationAuthority session as ``is_elevated``, so they can no longer disagree.
    """
    chat = 5555
    t = 3_000.0
    assert pin_gate.is_elevated(chat, now=t) is False
    assert pin_gate.is_verified(chat, now=t) is False

    pin_gate.elevate_session(chat, user_id=99, now=t)
    assert pin_gate.is_elevated(chat, now=t + 1) is True
    assert pin_gate.is_verified(chat, now=t + 1) is True  # B2: same store now

    pin_gate.lock_session(chat)
    assert pin_gate.is_elevated(chat, now=t + 2) is False
    assert pin_gate.is_verified(chat, now=t + 2) is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. security/owner_override.py — the *second* PIN impl (PBKDF2, file-stored)
# ─────────────────────────────────────────────────────────────────────────────


def _make_manager(tmp_path, pin: str = "2468"):
    from antigona.security.owner_override import OwnerOverrideManager

    m = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json")
    m.set_pin(pin)
    return m


def test_owner_override_wrong_pin_x5_locks_and_counts_separately(tmp_path) -> None:
    m = _make_manager(tmp_path)
    for i in range(1, 6):
        ok, msg = m.verify_and_elevate("cli", "owner", "s1", "9999", now=1_000.0 + i)
        assert ok is False
    assert m.is_locked_out(now=1_010.0) is True
    assert m.get_failed_attempts() == 5

    # right PIN refused while locked out
    ok, _ = m.verify_and_elevate("cli", "owner", "s1", "2468", now=1_011.0)
    assert ok is False

    # lockout clears after DEFAULT_LOCKOUT_TTL_SECONDS (900s)
    ok, _ = m.verify_and_elevate("cli", "owner", "s1", "2468", now=1_011.0 + 901.0)
    assert ok is True


def test_owner_override_lockout_SURVIVES_restart(tmp_path) -> None:
    """BEHAVIOUR DELTA — Wave B (CP-2).

    Before Wave B this test was ``..._does_NOT_survive_restart`` and asserted a
    fresh manager was NOT locked (RAM counters). Wave B moved the lockout to a
    durable store (ElevationAuthority, SQLite next to owner_pin.json), so a
    restart now KEEPS the brute-force lockout. Same intent (restart behaviour of
    the lockout), flipped expectation.
    """
    m1 = _make_manager(tmp_path)
    for i in range(5):
        m1.verify_pin("9999", now=100.0 + i)
    assert m1.is_locked_out(now=110.0) is True

    # "restart" = brand-new manager reading the SAME pin file (and the same
    # durable elevation store beside it)
    from antigona.security.owner_override import OwnerOverrideManager

    m2 = OwnerOverrideManager(pin_file_path=tmp_path / "owner_pin.json")
    assert m2.is_locked_out(now=110.0) is True, "durable lockout must survive restart"
    assert m2.verify_pin("2468", now=111.0) is False, "still locked right after restart"
    # and it still clears on its own once the window elapses
    assert m2.verify_pin("2468", now=110.0 + 901.0) is True


def test_owner_override_active_sessions_is_the_store_policyengine_reads(tmp_path) -> None:
    """CP-7 (still PARTIAL after Wave B): PolicyEngine.check reads
    owner_override.is_elevated((channel,user_id,session_id)).

    Wave B made the elevation store durable and shared (ElevationAuthority) and
    wired the CLI PIN gate into it, but the executor-side wiring
    (PolicyEngine(owner_override=...)) is deferred to an executor-touching wave.
    The session contract asserted here is unchanged.
    """
    m = _make_manager(tmp_path)
    assert m.is_elevated("telegram", "12345", "sess-1") is False
    ok, _ = m.verify_and_elevate("telegram", "12345", "sess-1", "2468", now=5.0)
    assert ok is True
    assert m.is_elevated("telegram", "12345", "sess-1", now=6.0) is True
    # wrong session_id -> not elevated (triple-scoped key)
    assert m.is_elevated("telegram", "12345", "sess-OTHER", now=6.0) is False
    # /lock one session
    assert m.lock_session("telegram", "12345", "sess-1") is True
    assert m.is_elevated("telegram", "12345", "sess-1", now=7.0) is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. security/approval_grant.py — TTL, replay, double-consume, purge (CP-1)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def grant_store(tmp_path):
    from antigona.security.approval_grant import ApprovalGrantStore

    return ApprovalGrantStore(db_path=tmp_path / "grants.db")


def test_approval_grant_ttl_expiry(grant_store) -> None:
    from antigona.security.approval_grant import GrantDenialReason

    tok = grant_store.issue(
        actor="owner", tool_name="sandbox.shell", args={"cmd": "ls"},
        issuer="test", ttl_seconds=300, now=1000.0,
    )
    assert grant_store.verify(tok, actor="owner", tool_name="sandbox.shell",
                              args={"cmd": "ls"}, now=1200.0).valid is True
    v = grant_store.verify(tok, actor="owner", tool_name="sandbox.shell",
                           args={"cmd": "ls"}, now=1000.0 + 301.0)
    assert v.valid is False and v.reason == GrantDenialReason.EXPIRED


def test_approval_grant_replay_blocked_after_consume(grant_store) -> None:
    from antigona.security.approval_grant import GrantDenialReason

    tok = grant_store.issue(actor="owner", tool_name="t", args={}, issuer="x", now=0.0)
    first = grant_store.verify_and_consume(tok, actor="owner", tool_name="t", args={}, now=1.0)
    assert first.valid is True
    replay = grant_store.verify_and_consume(tok, actor="owner", tool_name="t", args={}, now=2.0)
    assert replay.valid is False and replay.reason == GrantDenialReason.CONSUMED


def test_approval_grant_args_binding(grant_store) -> None:
    from antigona.security.approval_grant import GrantDenialReason

    tok = grant_store.issue(actor="owner", tool_name="t", args={"path": "a"}, issuer="x", now=0.0)
    v = grant_store.verify(tok, actor="owner", tool_name="t", args={"path": "DIFFERENT"}, now=1.0)
    assert v.valid is False and v.reason == GrantDenialReason.ARGS_MISMATCH


def test_approval_grant_purge_expired_works_but_is_unwired(grant_store) -> None:
    """CP-1: purge_expired() is correct — but grep shows NO caller in src/."""
    for i in range(3):
        grant_store.issue(actor="o", tool_name=f"t{i}", args={}, issuer="x",
                          ttl_seconds=10, now=100.0)
    removed = grant_store.purge_expired(now=100.0 + 11.0)
    assert removed == 3

    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits = subprocess.run(
        [sys.executable, "-c",
         "import pathlib,re,sys;"
         "p=pathlib.Path('src/antigona');"
         "print(sum(1 for f in p.rglob('*.py') "
         "for l in f.read_text(encoding='utf-8',errors='ignore').splitlines() "
         "if 'purge_expired' in l and 'def purge_expired' not in l))"],
        cwd=root, capture_output=True, text=True,
    )
    caller_lines = int((hits.stdout or "0").strip() or "0")
    assert caller_lines == 0, (
        f"purge_expired now has {caller_lines} caller line(s) in src/ — "
        "update CP-1 in the inventory"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. core/owner_gate.py — approvals table has NO TTL; double-decide is idempotent
# ─────────────────────────────────────────────────────────────────────────────


def _owner_gate_setup(tmp_path, name: str):
    from antigona.core.evidence_registry import EvidenceRegistry
    from antigona.core.owner_gate import OwnerGate
    from antigona.core.task_registry import TaskRegistry
    from antigona.database import Database
    from antigona.models import TaskState

    db = Database(f"sqlite:///{tmp_path / name}")
    db.create_all()
    session = db.session_factory()
    reg = TaskRegistry(session)
    created = reg.create(owner_id="owner-1", goal="g", idempotency_key=f"i-{name}",
                         correlation_id=f"c-{name}-1")
    for st, c in [(TaskState.QUEUED, 2), (TaskState.PLANNING, 3), (TaskState.WAITING_APPROVAL, 4)]:
        reg.update_status(created.task_id, st, actor="w", correlation_id=f"c-{name}-{c}")
    gate = OwnerGate(session, evidence_registry=EvidenceRegistry(session))
    return db, session, gate, created.task_id


def test_owner_gate_pending_has_no_ttl_field_or_expiry_api(tmp_path) -> None:
    """CP-1: an unanswered approval stays PENDING forever — no expiry path exists."""
    from antigona.core import owner_gate as og
    from antigona.models import Approval

    db, session, gate, task_id = _owner_gate_setup(tmp_path, "og_ttl.db")
    try:
        aid = gate.open_approval(task_id, "sandbox.shell", {"cmd": "ls"}, "CRITICAL",
                                 "why", "corr-x")
        row = session.get(Approval, aid)
        assert row.decision == "PENDING"
        # no time-to-live column, no expire/purge method anywhere on the gate
        assert not hasattr(gate, "purge_expired")
        assert not hasattr(gate, "expire_stale")
        assert "expires_at" not in Approval.__table__.columns
        # module exposes no sweeper function either
        assert not any(n for n in dir(og) if "purge" in n.lower() or "expire" in n.lower())
    finally:
        db.dispose()


def test_owner_gate_double_decide_is_idempotent(tmp_path) -> None:
    from antigona.core.owner_gate import GateDecision

    db, session, gate, task_id = _owner_gate_setup(tmp_path, "og_double.db")
    try:
        aid = gate.open_approval(task_id, "t", {}, "SENSITIVE", "why", "corr-1")
        d1 = gate.decide(aid, owner_user_id=7, decision=True, correlation_id="corr-2")
        d2 = gate.decide(aid, owner_user_id=7, decision=False, correlation_id="corr-3")
        assert d1 is GateDecision.APPROVED
        assert d2 is GateDecision.APPROVED, "second decide is a no-op; first decision stands"
    finally:
        db.dispose()
