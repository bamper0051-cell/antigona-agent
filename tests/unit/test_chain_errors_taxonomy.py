"""The typed-error taxonomy as executable invariants (wave G1b, M2).

PLAN.md §5.1/§5.3: one lost-CAS identity, one genuinely narrow contention sentinel,
and a decision table where "proven not started" is exactly one condition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.chain.errors import (
    ChainError,
    LockContendedError,
    LockPreAcquisitionError,
    StaleHeadError,
)
from antigona.chain.locking import acquire_store_lock
from antigona.chain.records import ChainRecord
from antigona.chain.store import ChainStore
from antigona.durable.state_machine import ConcurrentUpdate
from antigona.transport.telegram import PidLockError, acquire_pid_lock


def _record() -> ChainRecord:
    return ChainRecord(
        operation="review/start",
        previous_revision="",
        payload={"state": "reviewing"},
    )


# ── 35. no fifth lost-CAS identity ──────────────────────────────────────────


def test_stale_head_error_is_a_concurrent_update() -> None:
    assert issubclass(StaleHeadError, ConcurrentUpdate)
    assert issubclass(StaleHeadError, ChainError)
    # The narrow caveat is part of the contract, not an implementation detail.
    assert "NOT a proof of non-mutation" in (StaleHeadError.__doc__ or "")


# ── 36. contention is never a lost CAS ──────────────────────────────────────


def test_lock_contended_error_is_not_a_concurrent_update() -> None:
    assert not issubclass(LockContendedError, ConcurrentUpdate)
    assert not issubclass(LockContendedError, StaleHeadError)
    assert not issubclass(StaleHeadError, LockContendedError)
    assert not issubclass(LockPreAcquisitionError, LockContendedError)
    # The narrow scope of the contention claim is documented where callers read it.
    doc = LockContendedError.__doc__ or ""
    assert "never ran" in doc
    assert "does NOT prove" in doc or "NOT prove" in doc


# ── 37. the decision table ──────────────────────────────────────────────────


def test_lost_cas_is_never_classified_as_proven_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each §5.3 row: the "proven not started" column is exactly LockContendedError."""

    # Row: advisory lock already held -> proven not started.
    lock_path = tmp_path / "LOCK"
    holder = acquire_store_lock(lock_path)
    try:
        with pytest.raises(LockContendedError):
            acquire_store_lock(lock_path)
    finally:
        holder.release()

    # Row: mkdir/open failure -> not started, but NOT a busy proof.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(LockPreAcquisitionError) as excinfo:
        acquire_store_lock(blocker / "LOCK")
    assert not isinstance(excinfo.value, LockContendedError)

    # Row: flock raised a non-contention OSError -> not started.
    def _boom(fd: int, operation: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("antigona.chain.locking.fcntl.flock", _boom)
    with pytest.raises(LockPreAcquisitionError) as flock_error:
        acquire_store_lock(tmp_path / "LOCK2")
    assert not isinstance(flock_error.value, LockContendedError)
    monkeypatch.undo()

    # Row: HEAD != expected_revision -> unknown mutation, never contention.
    store = ChainStore(tmp_path / "chain")
    genesis = store.append("", _record())
    with pytest.raises(StaleHeadError) as stale:
        store.append("sha256:" + "0" * 64, _record())
    assert not isinstance(stale.value, LockContendedError)
    assert isinstance(stale.value, ConcurrentUpdate)
    assert genesis not in ("", stale.value.candidate)


# ── 38. the legacy pid-lock identity and contract are untouched ──────────────


def test_pid_lock_error_identity_is_unchanged(tmp_path: Path) -> None:
    assert issubclass(PidLockError, RuntimeError)
    assert not issubclass(PidLockError, ChainError)
    assert not issubclass(PidLockError, LockContendedError)
    assert not issubclass(PidLockError, ConcurrentUpdate)

    # Decision: acquire_pid_lock keeps its legacy None-on-contention contract.
    pid_path = str(tmp_path / "bot.pid")
    lock = acquire_pid_lock(pid_path)
    assert lock is not None
    try:
        assert acquire_pid_lock(pid_path) is None
    finally:
        lock.close()
