"""PHASE 4 — WORKSPACE OWNERSHIP v2 · INV-05 Lease Hardening (TTL / renew / expiry / dead-owner recovery / safe takeover).

Invariants covered (see wave docs 02_REQUIREMENTS.md / 03_STATE_MACHINE.md / 07_FENCING.md):
  * T-L1  renew extends expires_at WITHOUT changing owner or fencing epoch (INV-05 / FR-3).
  * T-L2  renew is conditional-atomic — only the current holder at the current epoch,
          with a live (unexpired) lease, may renew; wrong owner / stale epoch / expired
          lease / non-held repo are all denied (FR-3 T3/T4; EXPIRED->RENEWING forbidden).
  * T-L3  an expired lease is NOT ownership (INV-05): is_expired() is True, the holder is
          not verified as current, and verify_before_write is DENIED_EXPIRED.
  * T-L4  dead-owner recovery (reconcile) frees an EXPIRED lease back to FREE so a new
          owner can safely acquire with a NEW epoch; the old owner's token is fenced.
          Expired is NEVER revived (INV-07 restart safety).
  * T-L5  reconcile is a no-op on a live HELD lease and on an already-free repo.
  * T-L6  safe takeover supersedes EXPIRED with a NEW epoch; never overwrites a live
          HELD lease in place (03_STATE_MACHINE.md safe-takeover; INV-01/INV-03).
  * T-L7  verify_ownership confirms the current holder + epoch (read-only); it is
          distinct from verify_before_write (the write fence).
  * T-L8  state_of() reflects the local FREE/HELD/EXPIRED subset of the 8-state machine,
          deriving EXPIRED from the lease clock.
  * T-L9  bounded clock: single-authority local clock; a HELD lease with no explicit
          expires_at is ambiguous and treated as expired (fail-closed, INV-06).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from antigona.ownership.epoch import (
    EpochLedger,
    FenceStatus,
    OwnershipBusyError,
    OwnershipCheck,
)


def _repo() -> str:
    return str(uuid.uuid4())


# ── T-L1 / T-L2 renew ────────────────────────────────────────────────────────


def test_TL1_renew_extends_expiry_without_changing_owner_or_epoch(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        original_epoch = a.fencing_epoch
        original_owner = a.owner_id
        original_expiry = a.expires_at

        later = a.expires_at - timedelta(seconds=10)  # renew while still live
        renewed = ledger.renew(repo, "owner-a", original_epoch, extra_seconds=120, now=later)
        assert renewed is True
        assert ledger.current_epoch(repo) == original_epoch  # epoch UNCHANGED (INV-05/INV-03)
        assert ledger.current_owner(repo) == original_owner  # owner UNCHANGED
        new_expiry = ledger.lease_expiry(repo)
        assert new_expiry is not None
        assert new_expiry == later + timedelta(seconds=120)  # extended by extra_seconds
        assert new_expiry > original_expiry
    finally:
        ledger.close()


def test_TL2_renew_is_conditional_atomic_wrong_owner_denied(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        assert ledger.renew(repo, "owner-mallory", a.fencing_epoch, extra_seconds=60) is False
        assert ledger.renew(repo, "owner-a", a.fencing_epoch + 1, extra_seconds=60) is False
        assert (
            ledger.renew(repo, "owner-a", a.fencing_epoch, extra_seconds=60) is True
        )  # holder+epoch ok
        # non-held / unknown repo
        assert ledger.renew(_repo(), "owner-a", 1, extra_seconds=60) is False
    finally:
        ledger.close()


def test_TL2b_renew_denied_when_lease_expired_not_revived(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        after_expiry = a.expires_at + timedelta(seconds=1)
        # EXPIRED -> RENEWING is FORBIDDEN: renew must NOT revive the dead lease (INV-07).
        assert (
            ledger.renew(repo, "owner-a", a.fencing_epoch, extra_seconds=120, now=after_expiry)
            is False
        )
        # the lease is not revived: still not a live holder
        assert ledger.is_expired(repo, now=after_expiry) is True
    finally:
        ledger.close()


# ── T-L3 expired != ownership ───────────────────────────────────────────────


def test_TL3_expired_lease_is_not_ownership(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        after = a.expires_at + timedelta(seconds=1)
        assert ledger.is_expired(repo, now=after) is True
        own: OwnershipCheck = ledger.verify_ownership(repo, a.owner_id, a.fencing_epoch, now=after)
        assert own.is_holder is False
        assert own.is_expired is True
        fence = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch, now=after)
        assert fence.allowed is False
        assert fence.status == FenceStatus.DENIED_EXPIRED
    finally:
        ledger.close()


# ── T-L4 / T-L5 dead-owner recovery ─────────────────────────────────────────


def test_TL4_reconcile_frees_expired_lease_new_owner_wins_with_new_epoch(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        dead = ledger.acquire(repo, "owner-dead", lease_seconds=60)
        after = dead.expires_at + timedelta(seconds=1)

        # BEFORE recovery, the expired dead owner still occupies the record.
        assert ledger.is_expired(repo, now=after) is True
        assert ledger.state_of(repo, now=after) == "EXPIRED"

        # Dead-owner recovery frees it back to FREE.
        assert ledger.reconcile(repo, now=after) is True
        assert ledger.state_of(repo) == "FREE"

        # A new owner safely acquires with a strictly NEW epoch (INV-03).
        fresh = ledger.acquire(repo, "owner-alive")
        assert fresh.fencing_epoch == dead.fencing_epoch + 1
        assert ledger.current_epoch(repo) == fresh.fencing_epoch

        # The dead owner's old token is fenced (INV-04 / INV-07 expired != revived).
        fence = ledger.verify_before_write(repo, dead.owner_id, dead.fencing_epoch)
        assert fence.allowed is False
        assert fence.status == FenceStatus.DENIED_STALE_FENCE
    finally:
        ledger.close()


def test_TL5_reconcile_noop_on_live_lease_and_free_repo(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        assert ledger.reconcile(repo, now=a.expires_at - timedelta(seconds=1)) is False  # live
        assert ledger.state_of(repo) == "HELD"

        assert ledger.reconcile(_repo()) is False  # never-acquired repo
        ledger.release(repo, "owner-a")
        assert ledger.reconcile(repo) is False  # already FREE
    finally:
        ledger.close()


# ── T-L6 safe takeover ──────────────────────────────────────────────────────


def test_TL6_safe_takeover_never_overwrites_live_lease(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        with pytest.raises(OwnershipBusyError):
            ledger.takeover(repo, "owner-b")  # live HELD lease is NOT overwritten in place

        after = a.expires_at + timedelta(seconds=1)
        t = ledger.takeover(repo, "owner-b", now=after)
        assert t.fencing_epoch == a.fencing_epoch + 1  # NEW epoch (INV-03)
        assert ledger.current_epoch(repo) == a.fencing_epoch + 1
        assert ledger.current_owner(repo) == "owner-b"
    finally:
        ledger.close()


# ── T-L7 ownership verification (distinct from write fence) ────────────────


def test_TL7_verify_ownership_confirms_current_holder_and_epoch(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        own = ledger.verify_ownership(repo, a.owner_id, a.fencing_epoch)
        assert own.is_holder is True
        assert own.is_expired is False
        assert own.current_holder == "owner-a"
        assert own.current_epoch == a.fencing_epoch

        assert ledger.verify_ownership(repo, "owner-b", a.fencing_epoch).is_holder is False
        assert ledger.verify_ownership(repo, a.owner_id, a.fencing_epoch + 1).is_holder is False
        assert ledger.verify_ownership(_repo(), "owner-a", 1).is_holder is False
    finally:
        ledger.close()


# ── T-L8 state_of mapping ───────────────────────────────────────────────────


def test_TL8_state_of_reflects_free_held_expired(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        assert ledger.state_of(_repo()) == "FREE"
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        assert ledger.state_of(repo) == "HELD"
        after = a.expires_at + timedelta(seconds=1)
        assert ledger.state_of(repo, now=after) == "EXPIRED"
        ledger.release(repo, "owner-a")
        assert ledger.state_of(repo) == "FREE"
    finally:
        ledger.close()


# ── T-L9 bounded clock / fail-closed ────────────────────────────────────────


def test_TL9_held_lease_without_explicit_expiry_is_ambiguous_fail_closed(tmp_path: Path) -> None:
    # A HELD record with NO expires_at violates INV-05 (explicit lifetime). The local
    # layer treats that ambiguity as expired (fail-closed) and refuses ownership.
    from sqlalchemy import insert

    from antigona.ownership.epoch import _STATE_HELD, WorkspaceEpochRow

    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        # corrupt: clear the expiry directly on the row (simulate a partial write)
        with ledger._session() as session:
            session.execute(
                insert(WorkspaceEpochRow)
                .values(
                    repo_uuid=repo,
                    owner_id="owner-a",
                    fencing_epoch=a.fencing_epoch,
                    lease_id=a.lease_id,
                    issued_at=None,
                    expires_at=None,
                    state=_STATE_HELD,
                )
                .prefix_with("OR REPLACE")
            )
        assert ledger.is_expired(repo) is True
        assert ledger.state_of(repo) == "EXPIRED"
        fence = ledger.verify_before_write(repo, "owner-a", a.fencing_epoch)
        assert fence.allowed is False  # fail-closed
    finally:
        ledger.close()
