"""DF-WO2-003 — ownership gate wired into the LIVE workspace-creation path.

GROK/ANTIGRAVITY P0: the ownership mechanism was built but nothing in the
production path called it — ``bind_ownership`` was never invoked, so
``LocalWorkspace._ownership is None`` and protected writes ran UNFENCED.

This test exercises the REAL wired path (WorkspaceFactory.create_workspace →
LocalWorkspace with the gate bound when ANTIGONA_OWNERSHIP_ENABLED=1):

  * T-W1 (P0 acceptance / GROK): a STALE writer attempting a real agent
    ``write_file`` after a takeover is DENIED with NO side effect (the file is
    never created).  RED before this wiring (factory created an unfenced
    workspace, the write succeeded); GREEN after.
  * T-W2 fail-closed on busy (INV-06): a second workspace for a repo already held
    by another live owner is bound to a DENY-ALL context — its protected write is
    DENIED, never an unfenced no-op.
  * T-W3 dead-owner recovery (INV-07/FR-3): the wired reconciler frees an expired
    lease so a new owner acquires at a NEW (higher) epoch.
  * T-W4 backward-compat: with ownership OFF (default), create_workspace behaves
    exactly as before — no ownership bound, writes pass (no regression).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from antigona.config import Settings
from antigona.ownership.central import CentralAuthority
from antigona.ownership.epoch import EpochLedger, FenceDeniedError
from antigona.ownership.identity import resolve_repo_identity
from antigona.ownership.wiring import (
    reconcile_repo,
    release_workspace_ownership,
    shared_ledger_path,
)
from antigona.workspace import LocalWorkspace, WorkspaceFactory


def _settings(root: Path, ownership: bool = True) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        workspace=root,
        workspace_backend="local",
        ownership_enabled=ownership,
        ownership_dir=str(root / "shared"),
    )


def _repo_uuid(root: Path) -> str:
    return resolve_repo_identity(root).repo_uuid


def _close_workspace(ws: LocalWorkspace) -> None:
    ctx = getattr(ws, "_ownership", None)
    if ctx is not None:
        ctx.ledger.close()


# ── T-W1 P0 acceptance: stale writer fenced through the real live path ──────


def test_TW1_live_path_fences_stale_writer(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ownership=True)
    ws = WorkspaceFactory.create_workspace(config=settings)
    assert isinstance(ws, LocalWorkspace)
    assert ws._ownership is not None, "ownership gate was NOT bound by the factory"

    # Live owner's write is fenced AND permitted.
    res = ws.write_file("owner.txt", "hello")
    assert res.content == "hello"

    # A second owner/process takes over on the SAME shared ledger -> epoch N+1.
    repo = _repo_uuid(tmp_path)
    ledger2 = EpochLedger(shared_ledger_path(repo, settings))
    authority2 = CentralAuthority(ledger2)
    epoch1 = ws._ownership.fencing_epoch
    try:
        b = authority2.takeover(
            repo,
            "owner-B",
            now=ws._ownership.ledger.lease_expiry(repo)
            + __import__("datetime").timedelta(seconds=1),
        )
        assert b.fencing_epoch == epoch1 + 1

        # The STALE writer (owner1@epoch1) attempts a real agent write_file:
        # the mutation boundary consults the authority atomically -> DENIED.
        with pytest.raises(FenceDeniedError) as exc_info:
            ws.write_file("stale.txt", "must-not-appear")
        assert not exc_info.value.check.allowed
        # INV-04: provably no side effect.
        assert not (ws.root_path / "stale.txt").exists()
        # INV-09: the denial is audited.
        events = authority2.audit_history(repo)
        assert any(e.decision.startswith("DENIED") for e in events)
    finally:
        ledger2.close()
        _close_workspace(ws)


# ── T-W2 fail-closed on busy (INV-06): never an unfenced no-op ──────────────


def test_TW2_busy_workspace_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ownership=True)
    ws_a = WorkspaceFactory.create_workspace(config=settings)
    ws_b = WorkspaceFactory.create_workspace(config=settings)
    assert ws_a._ownership is not None
    assert ws_b._ownership is not None
    try:
        # B is bound to a DENY-ALL context: its protected write is refused with
        # no side effect, instead of silently writing unfenced.
        with pytest.raises(FenceDeniedError):
            ws_b.write_file("denied.txt", "x")
        assert not (ws_b.root_path / "denied.txt").exists()
        # The live owner still writes fine.
        ws_a.write_file("ok.txt", "y")
        assert (ws_a.root_path / "ok.txt").exists()
    finally:
        _close_workspace(ws_a)
        _close_workspace(ws_b)


def test_task_scoped_release_allows_approval_resume_for_same_worker(tmp_path: Path) -> None:
    """T04 regression: approval resume gets a valid epoch, never deny epoch 0."""
    settings = _settings(tmp_path, ownership=True)
    first = WorkspaceFactory.create_workspace(config=settings, task_id="flow", owner_id="worker")
    assert isinstance(first, LocalWorkspace)
    assert first.ownership is not None
    first_epoch = first.ownership.fencing_epoch
    assert release_workspace_ownership(first.ownership) is True

    resumed = WorkspaceFactory.create_workspace(
        config=settings, task_id="flow", owner_id="worker"
    )
    assert isinstance(resumed, LocalWorkspace)
    assert resumed.ownership is not None
    try:
        assert resumed.ownership.fencing_epoch == first_epoch + 1
        resumed.write_file("seed.txt", "one effect\n")
        assert (resumed.root_path / "seed.txt").read_text() == "one effect\n"
    finally:
        release_workspace_ownership(resumed.ownership)


# ── T-W3 dead-owner recovery wired (INV-07/FR-3) ────────────────────────────


def test_TW3_reconciler_frees_dead_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ownership=True)
    ws_a = WorkspaceFactory.create_workspace(config=settings, ownership_lease_seconds=1)
    epoch1 = ws_a._ownership.fencing_epoch
    try:
        # Dead-owner recovery: an expired lease is freed back to FREE.  Reconcile
        # at a time AFTER ws_a's 1s lease has lapsed (simulating a dead owner).
        repo = _repo_uuid(tmp_path)
        expired_at = ws_a._ownership.ledger.lease_expiry(repo)
        reconcile_repo(tmp_path, settings, now=expired_at + timedelta(seconds=1))
        # A fresh workspace for the same repo acquires at a NEW (higher) epoch.
        ws_b = WorkspaceFactory.create_workspace(config=settings)
        assert ws_b._ownership is not None
        assert ws_b._ownership.fencing_epoch == epoch1 + 1
        ws_b.write_file("recovered.txt", "new-owner")
        assert (ws_b.root_path / "recovered.txt").exists()
        _close_workspace(ws_b)
    finally:
        _close_workspace(ws_a)


# ── T-W4 backward-compat: ownership OFF changes nothing ─────────────────────


def test_TW4_disabled_by_default_is_unfenced(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ownership=False)
    ws = WorkspaceFactory.create_workspace(config=settings)
    assert isinstance(ws, LocalWorkspace)
    assert ws._ownership is None, "ownership must not be bound when disabled"
    # Behaviour identical to pre-wiring: writes pass unencumbered.
    ws.write_file("plain.txt", "no-fence")
    assert (ws.root_path / "plain.txt").exists()
