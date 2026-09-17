"""Maintenance-lock release semantics (wave G1d).

Scope: the *release* path of ``ChainStore.append``.  The acquisition ordering was
fixed in G1c (both acquisitions inside the ``try``), so the leak-on-contention
cases below are pins that must stay green.  What G1d adds is that the two releases
are NESTED rather than sequential: if releasing the store lock fails, the
maintenance lock must still be released in the same ``finally``.

``StoreLock.release`` suppresses ``OSError`` and is idempotent (invariant L5), so a
raising release is today only reachable through a defect or a subclass — which is
exactly why the hardening must not depend on ``release`` never raising.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

from antigona.chain import (
    ChainRecord,
    ChainStore,
    LockContendedError,
    StaleHeadError,
    StoreLock,
    acquire_store_lock,
)

_OPERATION = "c11/deployment-integrity"


def _store(tmp_path: Path) -> tuple[ChainStore, Path]:
    maintenance = tmp_path / "MAINT"
    return ChainStore(tmp_path / "chain", maintenance_lock_path=maintenance), maintenance


def _record() -> ChainRecord:
    return ChainRecord(operation=_OPERATION, payload={"ok": True})


def test_a_raising_store_lock_release_still_releases_the_maintenance_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before G1d: sequential releases let the first failure skip the second.

    The store-lock release is made to fail *after* it has really released its own
    descriptor, which is the only way to isolate the ordering under test: on the
    sequential form the maintenance release is never reached, so the maintenance
    lock is still held (flock is process-scoped and the descriptor is a bare int —
    no ``__del__`` closes it) and the re-acquisition below raises
    ``LockContendedError``.
    """
    store, maintenance_path = _store(tmp_path)
    real_release = StoreLock.release

    def flaky_release(self: StoreLock) -> None:
        real_release(self)
        if self.path == store.lock_path:
            raise RuntimeError("store lock release failed")

    monkeypatch.setattr(StoreLock, "release", flaky_release)

    with pytest.raises(RuntimeError, match="store lock release failed"):
        store.append("", _record())

    monkeypatch.undo()
    holder = acquire_store_lock(maintenance_path)
    holder.release()


def test_store_lock_contention_does_not_leak_the_maintenance_lock(tmp_path: Path) -> None:
    """Pin (fixed in G1c): contention on the store lock must not strand MAINT."""
    store, maintenance_path = _store(tmp_path)
    blocker = acquire_store_lock(store.lock_path)
    try:
        with pytest.raises(LockContendedError):
            store.append("", _record())
    finally:
        blocker.release()

    holder = acquire_store_lock(maintenance_path)
    holder.release()


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/self/fd is Linux-only")
def test_store_lock_contention_does_not_leak_a_descriptor(tmp_path: Path) -> None:
    """Pin (fixed in G1c): the refused append leaks neither flock nor descriptor."""
    store, _maintenance_path = _store(tmp_path)
    before = len(os.listdir("/proc/self/fd"))

    blocker = acquire_store_lock(store.lock_path)
    try:
        with pytest.raises(LockContendedError):
            store.append("", _record())
    finally:
        blocker.release()

    after = len(os.listdir("/proc/self/fd"))
    assert after == before


def test_maintenance_holder_blocks_append_and_the_body_never_runs(tmp_path: Path) -> None:
    """Pin (exclusivity is documented, not a bug): a maintenance holder fails closed."""
    store, maintenance_path = _store(tmp_path)
    store.append("", _record())
    head_before = store.read_head()
    listing_before = sorted(p.name for p in store.events_dir.iterdir())

    holder = acquire_store_lock(maintenance_path)
    try:
        with pytest.raises(LockContendedError):
            store.append(head_before, _record())
    finally:
        holder.release()

    assert store.read_head() == head_before
    assert sorted(p.name for p in store.events_dir.iterdir()) == listing_before
    assert not [p.name for p in store.events_dir.iterdir() if p.name.startswith(".event-")]


def test_maintenance_lock_is_released_on_success_and_on_domain_refusal(tmp_path: Path) -> None:
    """Control: the leak window is only the pre-``try`` path, both other exits release."""
    store, maintenance_path = _store(tmp_path)
    store.append("", _record())
    probe = acquire_store_lock(maintenance_path)
    probe.release()

    with pytest.raises(StaleHeadError):
        store.append("sha256:" + "0" * 64, _record())
    probe = acquire_store_lock(maintenance_path)
    probe.release()


def test_no_shared_maintenance_mode_is_exposed(tmp_path: Path) -> None:
    """Pin (non-goal): exclusive hook only — no shared/reader mode is implemented."""
    parameters = set(inspect.signature(ChainStore.__init__).parameters)
    assert {"maintenance_lock_path", "self"} <= parameters
    assert not parameters & {"maintenance_mode", "shared", "exclusive"}

    store, maintenance_path = _store(tmp_path)
    store.append("", _record())
    holder = acquire_store_lock(maintenance_path)
    try:
        with pytest.raises(LockContendedError):
            store.append(store.read_head(), _record())
    finally:
        holder.release()
