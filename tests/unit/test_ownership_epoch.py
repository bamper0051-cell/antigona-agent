"""PHASE 3 — WORKSPACE OWNERSHIP v2 · INV-03 Monotonic Fencing Epoch + INV-04 Stale-Writer enforcement.

Invariants covered (see wave docs 02_REQUIREMENTS.md / 03_STATE_MACHINE.md):
  * T-E1  epoch is strictly monotonic across acquire/takeover; never reused (INV-03).
  * T-E2  acquire is atomic — exactly one of N concurrent callers wins (INV-01).
  * T-E3  the epoch ledger is durable across a restart (INV-07).
  * T-E4  the current holder with the current epoch is allowed to write (INV-03).
  * T-E5  a stale epoch (superseded by a newer acquire) is DENIED_STALE_FENCE (INV-03/INV-04).
  * T-E5b safe takeover supersedes an EXPIRED lease with a NEW epoch; a live HELD
          lease is never overwritten in place (03_STATE_MACHINE.md safe-takeover).
  * T-E6  an owner mismatch is DENIED_STALE_FENCE (INV-04).
  * T-E7  an expired lease is denied fail-closed (INV-05/INV-06).
  * T-E8  stale writer physically cannot mutate the filesystem via write_file —
          the protected write is fenced BEFORE any mutation (INV-04, no side effect).
  * T-E9  stale writer cannot mutate the filesystem via execute_command (INV-04).
  * T-E10 no-ownership-bound workspaces behave exactly as before (backward compat).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def _ctx(ledger: EpochLedger, repo: str, owner: str, epoch: int) -> OwnershipContext:
    return OwnershipContext(ledger=ledger, repo_uuid=repo, owner_id=owner, fencing_epoch=epoch)


# ── epoch monotonicity + atomicity (INV-03 / INV-01) ────────────────────────


def test_TE1_epoch_is_strictly_monotonic_and_never_reused(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a1 = ledger.acquire(repo, "owner-a")
        assert a1.fencing_epoch == 1
        assert a1.repo_uuid == repo and a1.owner_id == "owner-a"
        assert a1.lease_id and a1.expires_at > datetime.now(UTC)

        assert ledger.release(repo, "owner-a") is True
        a2 = ledger.acquire(repo, "owner-a")  # re-acquire after release
        assert a2.fencing_epoch == 2  # strictly greater, never reused

        assert ledger.release(repo, "owner-a") is True
        a3 = ledger.acquire(repo, "owner-b")  # a different owner after release
        assert a3.fencing_epoch == 3
        assert ledger.current_epoch(repo) == 3
    finally:
        ledger.close()


def test_TE2_concurrent_acquire_exactly_one_wins(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        n = 8
        barrier = threading.Barrier(n)
        results: list[str] = []
        lock = threading.Lock()

        def worker(worker_id: str) -> None:
            barrier.wait()
            try:
                res = ledger.acquire(repo, worker_id)
                with lock:
                    results.append(f"won:{res.fencing_epoch}")
            except OwnershipBusyError:
                with lock:
                    results.append("busy")

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r.startswith("won")]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(results) == n  # every caller got a definitive answer
        assert ledger.current_epoch(repo) == 1  # bumped exactly once
    finally:
        ledger.close()


def test_TE3_epoch_ledger_durable_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    repo = _repo()

    ledger = EpochLedger(db)
    ledger.acquire(repo, "owner-a")
    ledger.release(repo, "owner-a")
    epoch_before_close = ledger.current_epoch(repo)
    ledger.close()

    # "restart": a brand-new ledger object on the same durable path
    ledger2 = EpochLedger(db)
    try:
        assert ledger2.current_epoch(repo) == epoch_before_close == 1
        a2 = ledger2.acquire(repo, "owner-b")
        assert a2.fencing_epoch == 2  # monotonic across the restart boundary
    finally:
        ledger2.close()


# ── fencing check (INV-03 / INV-04 / INV-05 / INV-06) ──────────────────────


def test_TE4_current_holder_with_current_epoch_allowed(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        check = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch)
        assert check.allowed is True
        assert check.status == FenceStatus.ALLOWED
    finally:
        ledger.close()


def test_TE5_stale_epoch_denied_after_superseded_acquire(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        old = ledger.acquire(repo, "owner-a")  # epoch 1
        ledger.release(repo, "owner-a")
        new = ledger.acquire(repo, "owner-b")  # epoch 2 supersedes
        assert new.fencing_epoch == old.fencing_epoch + 1

        # owner-a's OLD epoch is now stale -> denied before any write
        stale: FenceCheck = ledger.verify_before_write(repo, old.owner_id, old.fencing_epoch)
        assert stale.allowed is False
        assert stale.status == FenceStatus.DENIED_STALE_FENCE
    finally:
        ledger.close()


def test_TE5b_safe_takeover_supersedes_expired_lease_only(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        old = ledger.acquire(repo, "owner-a", lease_seconds=60)
        # A LIVE HELD lease is never overwritten in place (safe takeover).
        with pytest.raises(OwnershipBusyError):
            ledger.acquire(repo, "owner-b")

        # Once owner-a's lease is EXPIRED, takeover issues a NEW epoch (2).
        takeover = ledger.takeover(repo, "owner-b", now=old.expires_at + timedelta(seconds=1))
        assert takeover.fencing_epoch == old.fencing_epoch + 1
        assert ledger.current_epoch(repo) == 2
        # The superseded owner-a is fenced.
        stale: FenceCheck = ledger.verify_before_write(repo, old.owner_id, old.fencing_epoch)
        assert stale.allowed is False
        assert stale.status == FenceStatus.DENIED_STALE_FENCE
    finally:
        ledger.close()


def test_TE6_owner_mismatch_denied(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a")
        check: FenceCheck = ledger.verify_before_write(repo, "owner-mallory", a.fencing_epoch)
        assert check.allowed is False
        assert check.status == FenceStatus.DENIED_STALE_FENCE
        assert "owner" in check.reason.lower()
    finally:
        ledger.close()


def test_TE7_expired_lease_denied_fail_closed(tmp_path: Path) -> None:
    ledger = EpochLedger(tmp_path / "ledger.db")
    repo = _repo()
    try:
        a = ledger.acquire(repo, "owner-a", lease_seconds=60)
        # 61s later the lease is expired -> holder no longer owns (INV-05)
        later = a.expires_at + timedelta(seconds=1)
        check: FenceCheck = ledger.verify_before_write(repo, a.owner_id, a.fencing_epoch, now=later)
        assert check.allowed is False
        assert check.status == FenceStatus.DENIED_EXPIRED
    finally:
        ledger.close()


# ── stale-writer enforcement on the protected write path (INV-04) ──────────


def test_TE8_stale_writer_cannot_mutate_filesystem_via_write_file(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ledger = EpochLedger(default_ledger_path(ws_root))
    repo = _repo()
    try:
        owner_a = ledger.acquire(repo, "owner-a")
        ws = LocalWorkspace(
            ws_root, ownership=_ctx(ledger, repo, owner_a.owner_id, owner_a.fencing_epoch)
        )

        # current holder writes fine
        ws.write_file("ok.txt", "hello")
        assert (ws_root / "ok.txt").read_text(encoding="utf-8") == "hello"

        # takeover supersedes owner-a -> owner-a is now a STALE writer
        ledger.takeover(repo, "owner-b", now=owner_a.expires_at + timedelta(seconds=1))
        assert ledger.current_epoch(repo) == owner_a.fencing_epoch + 1

        with pytest.raises(FenceDeniedError) as exc_info:
            ws.write_file("stale.txt", "must-not-appear")
        assert not exc_info.value.check.allowed

        # INV-04: provably NO side effect — the file was never created, and the
        # still-valid file is untouched.
        assert not (ws_root / "stale.txt").exists()
        assert (ws_root / "ok.txt").read_text(encoding="utf-8") == "hello"
    finally:
        ledger.close()


def test_TE9_stale_writer_cannot_mutate_filesystem_via_execute_command(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ledger = EpochLedger(default_ledger_path(ws_root))
    repo = _repo()
    try:
        owner_a = ledger.acquire(repo, "owner-a")
        ws = LocalWorkspace(
            ws_root, ownership=_ctx(ledger, repo, owner_a.owner_id, owner_a.fencing_epoch)
        )

        # owner-a becomes stale (superseded by owner-b with a NEW epoch)
        ledger.takeover(repo, "owner-b", now=owner_a.expires_at + timedelta(seconds=1))

        with pytest.raises(FenceDeniedError):
            ws.execute_command(["touch", "fenced_cmd.txt"])

        # INV-04: the command never ran -> the file it would have created is absent
        assert not (ws_root / "fenced_cmd.txt").exists()
    finally:
        ledger.close()


def test_TE10_no_ownership_bound_is_unchanged_backward_compat(tmp_path: Path) -> None:
    # A workspace with NO ownership bound behaves exactly as before (no gate).
    ws_root = tmp_path / "workspace"
    ws = LocalWorkspace(ws_root)
    ws.write_file("plain.txt", "x")
    assert (ws_root / "plain.txt").read_text(encoding="utf-8") == "x"
