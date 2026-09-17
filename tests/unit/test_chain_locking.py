"""Unit tests for the bounded advisory lock and its typed refusals (wave G1b, M2).

Invariants under test (PLAN.md §5.2/§5.3): L1 contention and pre-acquisition are
unrelated types, L2 contention is classified from the syscall, L4 a setup failure is
never reported as contention, L5 release is unconditional and idempotent.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from antigona.chain import locking
from antigona.chain.errors import LockContendedError, LockPreAcquisitionError


def _src_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "src")


# ── 28. same-process contention ─────────────────────────────────────────────


def test_contended_lock_raises_lock_contended_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "LOCK"
    holder = locking.acquire_store_lock(lock_path)
    try:
        with pytest.raises(LockContendedError) as excinfo:
            locking.acquire_store_lock(lock_path)
        assert str(lock_path) in str(excinfo.value)
    finally:
        holder.release()

    # Released -> reacquirable.
    again = locking.acquire_store_lock(lock_path)
    again.release()


# ── 29. cross-process contention ────────────────────────────────────────────


def test_contended_lock_across_processes_raises_lock_contended_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "LOCK"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"import sys, time; sys.path.insert(0, {_src_path()!r});"
                "from antigona.chain.locking import acquire_store_lock;"
                f"lock = acquire_store_lock({str(lock_path)!r});"
                "print('LOCKED', flush=True); time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert "LOCKED" in holder.stdout.readline()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                locking.acquire_store_lock(lock_path).release()
            except LockContendedError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("expected LockContendedError for a live cross-process holder")
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # Holder gone -> the lock is free again.
    locking.acquire_store_lock(lock_path).release()


# ── 30. L4 — an environment defect is not contention ────────────────────────


def test_read_only_directory_raises_pre_acquisition_not_contention(tmp_path: Path) -> None:
    # A parent component that is a regular file: mkdir fails ENOTDIR before the
    # lock can exist. (Root bypasses DAC, so ENOTDIR is the portable form of
    # "this path cannot be set up"; the EACCES form is exercised when unprivileged.)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(LockPreAcquisitionError) as excinfo:
        locking.acquire_store_lock(blocker / "LOCK")
    assert not isinstance(excinfo.value, LockContendedError)

    if os.geteuid() != 0:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            with pytest.raises(LockPreAcquisitionError) as unprivileged:
                locking.acquire_store_lock(readonly / "LOCK")
            assert not isinstance(unprivileged.value, LockContendedError)
        finally:
            readonly.chmod(0o700)


# ── 31. a non-contention flock error is pre-acquisition ─────────────────────


def test_non_contention_flock_error_is_pre_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(fd: int, operation: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(locking.fcntl, "flock", _boom)
    with pytest.raises(LockPreAcquisitionError) as excinfo:
        locking.acquire_store_lock(tmp_path / "LOCK")
    assert not isinstance(excinfo.value, LockContendedError)


# ── 32. L1 — the two refusal identities are unrelated ───────────────────────


def test_lock_contended_and_pre_acquisition_are_unrelated_types() -> None:
    assert not issubclass(LockContendedError, LockPreAcquisitionError)
    assert not issubclass(LockPreAcquisitionError, LockContendedError)


# ── 33. L2 — persisted metadata is never authorisation ──────────────────────


def test_stale_pid_metadata_does_not_grant_or_block_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "LOCK"
    lock_path.write_text('{"pid": 999999, "host": "dead-host"}\n')

    held = locking.acquire_store_lock(lock_path)
    try:
        assert held.owner.pid == os.getpid()
        # A live holder's metadata text grants nothing to the next caller.
        with pytest.raises(LockContendedError):
            locking.acquire_store_lock(lock_path)
    finally:
        held.release()

    # Release leaves the stale-text file behind; that text still blocks nothing.
    lock_path.write_text('{"pid": 999998, "host": "dead-host"}\n')
    locking.acquire_store_lock(lock_path).release()


# ── 34. L5 — release is idempotent and actually unlocks ─────────────────────


def test_release_is_idempotent_and_unlocks(tmp_path: Path) -> None:
    lock_path = tmp_path / "LOCK"
    lock = locking.acquire_store_lock(lock_path)
    lock.release()
    lock.release()

    other = locking.acquire_store_lock(lock_path)
    other.release()

    # context-manager form releases too
    with locking.acquire_store_lock(lock_path) as held:
        assert held.owner.pid == os.getpid()
    locking.acquire_store_lock(lock_path).release()
