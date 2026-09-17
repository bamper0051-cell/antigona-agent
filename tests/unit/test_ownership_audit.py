"""PHASE 5 — WORKSPACE OWNERSHIP v2 · INV-09 Durable append-only audit trail.

Invariants covered (see wave docs 02_REQUIREMENTS.md INV-09 / FR / 03_STATE_MACHINE.md):
  * T-A1  acquire records a durable audit event with the EXACT contract shape
          (event_id/timestamp/repo_uuid/owner_id/lease_id/fencing_epoch/action/
          decision/reason/correlation_id).
  * T-A2  the audit trail is append-only — entries are only ever INSERTed, never
          UPDATEd or DELETEd (enforced at the SQLite layer by triggers).
  * T-A3  chronological reconstruction (T13): A acquire -> A renew -> A expire ->
          B takeover -> A stale write denied reconstructs exactly that sequence,
          matching the actual ownership state.
  * T-A4  correlation_id groups a logical operation (acquire->write->release).
  * T-A5  a denied stale write records a write_check DENIED_STALE_FENCE event.
  * T-A6  an allowed write records a write_check ALLOWED event.
  * T-A7  renew / release (success + denied) are audited.
  * T-A8  reconcile (expiry -> free) records an expiry event (dead-owner recovery).
  * T-A9  callers that do NOT pass a correlation_id still work (backward compat);
          a correlation_id is auto-generated as a default.
  * T-A10 audit trail is durable across a ledger restart (INV-07).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from antigona.ownership.epoch import (
    EpochLedger,
    FenceStatus,
    OwnershipBusyError,
)


def _repo() -> str:
    return str(uuid.uuid4())


def _audit_table(db: Path) -> list[dict]:
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_id, repo_uuid, action, decision, correlation_id "
                "FROM workspace_ownership_audit ORDER BY event_id"
            )
        ).mappings()
        return [dict(r) for r in rows]
    eng.dispose()


# ── T-A1 exact contract shape on acquire ────────────────────────────────────

def test_TA1_acquire_records_contract_shaped_audit_event(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = EpochLedger(db)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        hist = ledger.audit_history(repo)
        assert len(hist) == 1
        ev = hist[0]
        # exact contract shape
        assert ev.repo_uuid == repo
        assert ev.owner_id == "owner-a"
        assert ev.lease_id == a.lease_id
        assert ev.fencing_epoch == a.fencing_epoch
        assert ev.action == "acquire"
        assert ev.decision == "SUCCESS"
        assert ev.reason
        assert ev.event_id > 0
        assert ev.timestamp is not None
        assert ev.correlation_id  # auto-generated
    finally:
        ledger.close()


# ── T-A2 append-only (enforced at the DB layer) ─────────────────────────────

def test_TA2_audit_trail_is_append_only_no_update_delete(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = EpochLedger(db)
    repo = _repo()
    try:
        ledger.acquire(repo, "owner-a")
        ledger.release(repo, "owner-a")
        count_before = len(ledger.audit_history(repo))
        assert count_before >= 2

        # Direct UPDATE / DELETE on the audit table must be rejected (append-only).
        eng = create_engine(f"sqlite:///{db}")
        with eng.begin() as conn:
            with pytest.raises(IntegrityError):  # SQLite trigger ABORT
                conn.execute(
                    text("UPDATE workspace_ownership_audit SET decision='HACKED' WHERE 1=1")
                )
        with eng.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text("DELETE FROM workspace_ownership_audit WHERE 1=1"))
        eng.dispose()

        # Nothing was altered.
        assert len(ledger.audit_history(repo)) == count_before
        assert all(e.decision != "HACKED" for e in ledger.audit_history(repo))
    finally:
        ledger.close()


# ── T-A3 chronological reconstruction (T13) ─────────────────────────────────

def test_TA3_chronological_reconstruction_matches_state(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        # A renews: renewed expiry = a.expires_at + 30s.
        assert ledger.renew(repo, "owner-a", a.fencing_epoch, extra_seconds=60,
                            now=a.expires_at - timedelta(seconds=30)) is True
        # A's (renewed) lease expires, then B takes over with a NEW epoch.
        b = ledger.takeover(repo, "owner-b", now=a.expires_at + timedelta(seconds=31))
        assert b.fencing_epoch == a.fencing_epoch + 1
        # A's stale write is denied.
        check = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch)
        assert check.allowed is False
        assert check.status == FenceStatus.DENIED_STALE_FENCE

        hist = ledger.audit_history(repo)
        events = [(e.action, e.decision, e.owner_id) for e in hist]
        # acquire -> renew -> takeover(acquire with new epoch) -> write_check denied
        assert events[0][0] == "acquire" and events[0][2] == "owner-a"
        assert events[1][0] == "renew"
        # takeover supersedes the expired A lease with a NEW epoch for B
        assert events[2][0] == "takeover" and events[2][1] == "SUCCESS" and events[2][2] == "owner-b"
        assert events[3][0] == "write_check" and events[3][1] == "DENIED_STALE_FENCE"
        # monotonic event_id ordering == chronological
        ids = [e.event_id for e in hist]
        assert ids == sorted(ids)
    finally:
        ledger.close()


# ── T-A4 correlation groups a logical operation ─────────────────────────────

def test_TA4_correlation_id_groups_acquire_write_release(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        corr = ledger.new_correlation_id()
        a = ledger.acquire(repo, "owner-a", correlation_id=corr)
        ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch, correlation_id=corr)
        ledger.release(repo, "owner-a", correlation_id=corr)

        grouped = [e for e in ledger.audit_history(repo) if e.correlation_id == corr]
        assert len(grouped) == 3  # acquire + write_check + release
        assert {e.action for e in grouped} == {"acquire", "write_check", "release"}
        # a different logical operation carries a distinct correlation
        assert len({e.correlation_id for e in ledger.audit_history(repo)}) == 1
    finally:
        ledger.close()


# ── T-A5 denied stale write audited ─────────────────────────────────────────

def test_TA5_denied_stale_write_audited(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        ledger.release(repo, "owner-a")
        ledger.acquire(repo, "owner-b")
        check = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch)
        assert check.status == FenceStatus.DENIED_STALE_FENCE
        denied = [e for e in ledger.audit_history(repo)
                  if e.action == "write_check" and e.decision.startswith("DENIED")]
        assert len(denied) == 1
        assert denied[0].decision == "DENIED_STALE_FENCE"
        assert denied[0].owner_id == a.owner_id
        assert denied[0].fencing_epoch == a.fencing_epoch
    finally:
        ledger.close()


# ── T-A6 allowed write audited ──────────────────────────────────────────────

def test_TA6_allowed_write_audited(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        check = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch)
        assert check.allowed is True
        assert check.status == FenceStatus.ALLOWED
        allowed = [e for e in ledger.audit_history(repo) if e.action == "write_check"]
        assert len(allowed) == 1
        assert allowed[0].decision == "ALLOWED"
    finally:
        ledger.close()


# ── T-A7 renew / release audited ────────────────────────────────────────────

def test_TA7_renew_and_release_audited(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        assert ledger.renew(repo, "owner-a", a.fencing_epoch, extra_seconds=60) is True
        # denied renew by a non-holder
        assert ledger.renew(repo, "mallory", a.fencing_epoch, extra_seconds=60) is False
        # denied release by a non-holder
        assert ledger.release(repo, "mallory") is False
        # success release by the holder
        assert ledger.release(repo, "owner-a") is True

        actions = [e.action for e in ledger.audit_history(repo)]
        assert "renew" in actions
        assert "release" in actions
        renew_ok = [e for e in ledger.audit_history(repo)
                    if e.action == "renew" and e.decision == "SUCCESS"]
        renew_denied = [e for e in ledger.audit_history(repo)
                        if e.action == "renew" and e.decision.startswith("DENIED")]
        release_ok = [e for e in ledger.audit_history(repo)
                      if e.action == "release" and e.decision == "SUCCESS"]
        release_denied = [e for e in ledger.audit_history(repo)
                          if e.action == "release" and e.decision.startswith("DENIED")]
        assert len(renew_ok) == 1
        assert len(renew_denied) == 1
        assert len(release_ok) == 1
        assert len(release_denied) == 1
    finally:
        ledger.close()


# ── T-A8 reconcile records expiry ───────────────────────────────────────────

def test_TA8_reconcile_records_expiry_on_dead_owner_recovery(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        after = a.expires_at + timedelta(seconds=1)
        assert ledger.reconcile(repo, now=after) is True  # EXPIRED -> FREE
        expiry = [e for e in ledger.audit_history(repo) if e.action == "expiry"]
        assert len(expiry) == 1
        assert expiry[0].decision == "RECONCILED_FREE"
        assert expiry[0].owner_id == "owner-a"
        # the freed repo is now FREE; a no-op reconcile records nothing new
        assert ledger.reconcile(repo, now=after) is False
        assert len(ledger.audit_history(repo)) == 2  # acquire + expiry
    finally:
        ledger.close()


# ── T-A9 backward compat: no correlation_id → auto default ─────────────────

def test_TA9_no_correlation_id_still_works_auto_generated(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")  # no correlation passed
        assert a.fencing_epoch == 1
        ev = ledger.audit_history(repo)[0]
        assert ev.correlation_id  # auto-generated, non-empty
        assert len(ev.correlation_id) > 8
    finally:
        ledger.close()


# ── T-A10 durable across restart ────────────────────────────────────────────

def test_TA10_audit_durable_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    repo = _repo()
    ledger = EpochLedger(db)
    ledger.acquire(repo, "owner-a")
    ledger.close()

    # restart on the same durable path
    ledger2 = EpochLedger(db)
    try:
        hist = ledger2.audit_history(repo)
        assert len(hist) == 1
        assert hist[0].action == "acquire" and hist[0].owner_id == "owner-a"
    finally:
        ledger2.close()


# ── T-A11 busy acquire is audited (fail-closed) ─────────────────────────────

def test_TA11_busy_acquire_is_audited(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        with pytest.raises(OwnershipBusyError):
            ledger.acquire(repo, "owner-b")
        busy = [e for e in ledger.audit_history(repo)
                if e.action == "acquire" and e.decision == "DENIED_BUSY"]
        assert len(busy) == 1
        assert busy[0].owner_id == "owner-b"
        assert busy[0].fencing_epoch == a.fencing_epoch  # current epoch at denial
    finally:
        ledger.close()
