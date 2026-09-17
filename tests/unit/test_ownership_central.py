"""PHASE 6 — WORKSPACE OWNERSHIP v2 · Central Authority (INV-01/INV-06/INV-08) + TOCTOU closure (DF-WO2-002).

Invariants covered (see wave docs 02_REQUIREMENTS.md / 03_STATE_MACHINE.md / defect_DF-WO2-002):
  * T-C1  INV-04 stale-writer acceptance: A verifies at epoch N (would pass), B takes
          over (epoch N+1), then A writes — must be DENIED_STALE_FENCE with NO side
          effect (the file is never created). (DF-WO2-002 described case.)
  * T-C2  TOCTOU race (deterministic): a takeover attempting to slip into the
          verify->write window is BLOCKED while A holds the fence across the
          mutation. The INV-04 invariant "ownership cannot change between the check
          and the side effect" HOLDS. (This test was RED under phase-3.)
  * T-C3  acquire_write_permit is an ATOMIC epoch+owner+lease check + HOLD: after a
          permit is granted, a concurrent takeover (which needs FREE/EXPIRED) is
          DENIED — the write window is provably fenced (FR-4/FR-6).
  * T-C4  INV-01/INV-08 central authority delegates the lifecycle and serializes
          cross-clone acquire (exactly one holder; second acquire DENIED_BUSY).
  * T-C5  INV-06 fail-closed: authority unavailable => CentralAuthorityFailure and the
          protected write is blocked with NO side effect.
  * T-C6  INV-04 write-permit denied for wrong owner / stale epoch / expired lease.
  * T-C7  INV-09 the reserved ``central_authority_failure`` audit event is emitted on
          fail-closed.
  * T-C8  the central authority exposes an atomic current epoch+owner+lease view
          (verify_before_write / verify_ownership).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from antigona.ownership.central import CentralAuthority, CentralAuthorityFailure
from antigona.ownership.epoch import (
    EpochLedger,
    FenceCheck,
    FenceDeniedError,
    FenceStatus,
    OwnershipBusyError,
    OwnershipContext,
    default_ledger_path,
)
from antigona.workspace import LocalWorkspace


def _repo() -> str:
    return str(uuid.uuid4())


def _central_ctx(
    ledger: EpochLedger, repo: str, owner: str, epoch: int, authority: CentralAuthority
) -> OwnershipContext:
    return OwnershipContext(
        ledger=ledger,
        repo_uuid=repo,
        owner_id=owner,
        fencing_epoch=epoch,
        central=authority,
    )


# ── T-C1 stale-writer-after-takeover acceptance (DF-WO2-002 described case) ─


def test_TC1_stale_write_after_takeover_denied_no_side_effect(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ledger = EpochLedger(default_ledger_path(ws_root))
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)

        # A verifies at epoch N (would pass).
        assert ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch).allowed is True

        ws = LocalWorkspace(
            ws_root, ownership=_central_ctx(ledger, repo, a.owner_id, a.fencing_epoch, authority)
        )

        # A's lease expires and B takes over -> epoch N+1.
        b = ledger.takeover(repo, "owner-b", now=a.expires_at + timedelta(seconds=1))
        assert b.fencing_epoch == a.fencing_epoch + 1

        # A writes WITHOUT re-verifying: the mutation boundary consults the
        # authority atomically -> DENIED_STALE_FENCE, provably no side effect.
        with pytest.raises(FenceDeniedError) as exc_info:
            ws.write_file("stale.txt", "must-not-appear")
        assert not exc_info.value.check.allowed
        assert exc_info.value.check.status == FenceStatus.DENIED_STALE_FENCE
        assert not (ws_root / "stale.txt").exists()
    finally:
        ledger.close()


# ── T-C2 TOCTOU race: takeover cannot slip into the verify->write window ────


def test_TC2_takeover_blocked_inside_verify_write_window(tmp_path: Path) -> None:
    """Deterministic TOCTOU reproduction (RED under phase-3, GREEN here).

    A's write begins (the mutation boundary acquires a write permit, holding the
    fence). A concurrent takeover attempts to slip in while the lease has REALLY
    expired. Because the authority HOLDS the fence across the mutation, the
    takeover cannot win — ownership cannot change between the check and the side
    effect (INV-04 / DF-WO2-002 closed).
    """
    ws_root = tmp_path / "workspace"
    ledger = EpochLedger(default_ledger_path(ws_root))
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=1)  # short lease => reachable gap
        ws = LocalWorkspace(
            ws_root, ownership=_central_ctx(ledger, repo, a.owner_id, a.fencing_epoch, authority)
        )

        proceed = threading.Event()
        original_write = ws._file_tools.write_text

        def blocking_write(path, content):
            proceed.wait(timeout=10)  # freeze A's mutation mid-flight
            return original_write(path, content)

        ws._file_tools.write_text = blocking_write

        outcome = {"takeover_won": None}

        def attempt_takeover() -> None:
            time.sleep(1.2)  # A's 1s lease REALLY expires
            try:
                ledger.takeover(repo, "owner-b")
                outcome["takeover_won"] = True  # BAD: slipped into the write window
            except OwnershipBusyError:
                outcome["takeover_won"] = False  # GOOD: fence held across the write
            finally:
                proceed.set()

        t = threading.Thread(target=attempt_takeover)
        t.start()
        ws.write_file("fenced.txt", "data")  # A's protected write
        t.join()

        assert outcome["takeover_won"] is False, (
            "TOCTOU: a takeover won inside A's verify->write window"
        )
        # A remains the holder (its permit held the fence), so its write is legitimate.
        assert (ws_root / "fenced.txt").read_text(encoding="utf-8") == "data"
    finally:
        ledger.close()


# ── T-C3 write permit is an atomic check + HOLD ─────────────────────────────


def test_TC3_write_permit_holds_fence_blocks_concurrent_takeover(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        permit = authority.acquire_write_permit(repo, a.owner_id, a.fencing_epoch)
        assert permit is not None
        assert permit.repo_uuid == repo
        assert permit.owner_id == "owner-a"
        assert permit.fencing_epoch == a.fencing_epoch
        assert permit.valid_until > datetime.now(UTC)

        # While A holds the permit, a takeover cannot win (lease is HELD + extended).
        with pytest.raises(OwnershipBusyError):
            ledger.takeover(repo, "owner-b")
        # And a fresh acquire also fails (INV-01).
        with pytest.raises(OwnershipBusyError):
            authority.acquire(repo, "owner-b")
        assert authority.current_owner(repo) == "owner-a"
        assert authority.current_epoch(repo) == a.fencing_epoch
    finally:
        ledger.close()


# ── T-C4 central authority delegates lifecycle + serializes clones (INV-01/08)


def test_TC4_central_authority_delegates_and_serializes_cross_clone(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = EpochLedger(db)
    repo = _repo()
    ledger2 = EpochLedger(db)  # "second clone" authority on the SAME durable store
    try:
        auth1 = CentralAuthority(ledger)
        auth2 = CentralAuthority(ledger2)

        a1 = auth1.acquire(repo, "clone-a")
        # clone #2 cannot acquire while clone #1 holds a valid lease (INV-01/INV-08).
        with pytest.raises(OwnershipBusyError):
            auth2.acquire(repo, "clone-b")
        assert auth2.current_owner(repo) == "clone-a"
        assert auth2.current_epoch(repo) == a1.fencing_epoch

        # renew / release / reconcile delegate through the authority.
        assert auth2.renew(repo, "clone-a", a1.fencing_epoch, extra_seconds=60) is True
        assert auth1.release(repo, "clone-a") is True
        assert auth2.state_of(repo) == "FREE"
        assert auth2.audit_history(repo)  # audit reconstructable via authority
    finally:
        ledger.close()
        ledger2.close()


# ── T-C5 fail-closed: authority unavailable blocks the write (INV-06) ───────


def test_TC5_fail_closed_authority_unavailable_blocks_write_no_side_effect(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ledger = EpochLedger(default_ledger_path(ws_root))
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        ws = LocalWorkspace(
            ws_root, ownership=_central_ctx(ledger, repo, a.owner_id, a.fencing_epoch, authority)
        )

        # Authority becomes unavailable for the write-permit operation.
        def _boom(*args: object, **kwargs: object) -> None:
            raise OperationalError("mock", None, Exception("authority down"))

        ledger.acquire_write_permit = _boom  # type: ignore[method-assign, assignment]

        with pytest.raises(CentralAuthorityFailure):
            ws.write_file("must-not.txt", "data")
        # INV-06: provably no side effect.
        assert not (ws_root / "must-not.txt").exists()
    finally:
        ledger.close()


# ── T-C6 write permit denied for wrong owner / stale epoch / expired (INV-04) ─


def test_TC6_write_permit_denied_stale_owner_epoch_expired(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)

        # wrong owner
        with pytest.raises(FenceDeniedError):
            authority.acquire_write_permit(repo, "mallory", a.fencing_epoch)
        # stale epoch
        with pytest.raises(FenceDeniedError):
            authority.acquire_write_permit(repo, a.owner_id, a.fencing_epoch + 1)
        # expired lease
        after = a.expires_at + timedelta(seconds=1)
        with pytest.raises(FenceDeniedError):
            authority.acquire_write_permit(repo, a.owner_id, a.fencing_epoch, now=after)
        # unknown repo
        with pytest.raises(FenceDeniedError):
            authority.acquire_write_permit(_repo(), a.owner_id, 1)
    finally:
        ledger.close()


# ── T-C7 central_authority_failure audit event (INV-09) ─────────────────────


def test_TC7_central_authority_failure_audited(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OperationalError("mock", None, Exception("authority down"))

        ledger.acquire_write_permit = _boom  # type: ignore[method-assign, assignment]

        with pytest.raises(CentralAuthorityFailure):
            authority.acquire_write_permit(repo, a.owner_id, a.fencing_epoch)

        failures = [
            e for e in ledger.audit_history(repo) if e.action == "central_authority_failure"
        ]
        assert len(failures) == 1
        assert failures[0].decision == "FAILED"
        assert failures[0].owner_id == "owner-a"
        assert "unavailable" in failures[0].reason.lower()
    finally:
        ledger.close()


# ── T-C8 central authority exposes atomic epoch+owner+lease view ────────────


def test_TC8_central_authority_atomic_current_state_view(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    authority = CentralAuthority(ledger)
    repo = _repo()
    try:
        assert authority.state_of(repo) == "FREE"
        a = authority.acquire(repo, "owner-a")
        # atomic read of current epoch+owner+lease
        check: FenceCheck = authority.verify_before_write(repo, a.owner_id, a.fencing_epoch)
        assert check.allowed is True
        own = authority.verify_ownership(repo, a.owner_id, a.fencing_epoch)
        assert own.is_holder is True
        assert own.current_epoch == a.fencing_epoch
        assert own.current_holder == "owner-a"
        assert authority.current_owner(repo) == "owner-a"
        assert authority.current_epoch(repo) == a.fencing_epoch
    finally:
        ledger.close()
