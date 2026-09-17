"""DF-WO2-003 — wire the ownership gate into the LIVE workspace-creation path.

WORKSPACE OWNERSHIP v2 (wave-wo2-20260821_051217).  The ownership mechanism
(identity / epoch / lease / audit / central) was fully built but nothing in the
production path called it: ``LocalWorkspace.bind_ownership`` was never invoked,
so ``LocalWorkspace._ownership is None`` and every protected write ran with NO
fencing.  This module is the single wiring choke point that connects the gate to
the real agent execution path.

It is invoked from ``WorkspaceFactory.create_workspace`` (the one boundary both
``worker/__init__.py`` dispatch and ``worker/agent_core.py`` go through), so a
single wiring point fences every agent-created workspace when ownership is
enabled.

Design (DF-WO2-003):

* **Enable flag** — ``ANTIGONA_OWNERSHIP_ENABLED`` env / ``Settings.ownership_enabled``,
  default OFF for backward-compat.  When off, ``bind_workspace_ownership`` returns
  ``None`` immediately and behaviour is byte-for-byte unchanged (no ledger, no
  identity marker, no fencing).

* **Stable shared ledger** — the logical repo identity is resolved on the BASE
  workspace root (``resolve_repo_identity``), NOT on a per-task subdir, so two
  clones/tasks of the same repo converge on one ``repo_uuid``.  The central
  authority is constructed on a SHARED, stable ledger path derived from that
  ``repo_uuid`` under a shared ownership dir (``ANTIGONA_OWNERSHIP_DIR`` /
  ``Settings.ownership_dir``, default ``~/.antigona/ownership``) — one durable
  ledger per logical repo, so all clones/processes of the repo serialize through
  one authority (INV-01 / INV-08, mirrors PHASE 7's shared-ledger insight).

* **Owner identity** — ``owner_id`` defaults to ``ANTIGONA_WORKER_ID`` (the live
  worker's id, which ``worker/__init__.py`` also uses) or a stable ``agent-<host>``
  id, and can be overridden explicitly by the caller.

* **Fail-closed on busy (INV-06)** — if ``acquire`` raises ``OwnershipBusyError``
  (another live holder) or the authority is unavailable, the workspace is bound
  to a DENY-ALL ``OwnershipContext`` (epoch 0, which can never match a real lease),
  so every protected write is DENIED with no side effect.  It NEVER silently
  no-ops when ownership is intended: an unfenced workspace is never produced.

* **Dead-owner recovery (INV-07 / FR-3)** — ``reconcile`` is called on the repo
  before each acquire, so an expired lease from a crashed/dead owner is freed back
  to FREE and the next live owner may acquire with a NEW epoch.  This wires the
  periodic reconciler at the natural workspace/agent-setup boundary; the same
  helper is exposed for an external periodic tick.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from antigona.core import paths

from .central import CentralAuthority
from .epoch import (
    EpochLedger,
    FenceCheck,
    FenceDeniedError,
    FenceStatus,
    OwnershipBusyError,
    OwnershipContext,
    WritePermit,
)
from .identity import resolve_repo_identity

if TYPE_CHECKING:  # pragma: no cover
    from antigona.config import Settings

logger = logging.getLogger(__name__)

#: Env switch — when absent/off the gate is inert (backward-compat).
ENV_ENABLED = "ANTIGONA_OWNERSHIP_ENABLED"
#: Env — shared ownership dir under which one ledger per ``repo_uuid`` is stored.
ENV_OWNERSHIP_DIR = "ANTIGONA_OWNERSHIP_DIR"
#: Env — live worker id (also used by ``worker/__init__.py`` for the ExecutionGuard).
ENV_WORKER_ID = "ANTIGONA_WORKER_ID"

#: Default lease lifetime for the agent's ownership lease.
DEFAULT_LEASE_SECONDS = 60
#: A fence token that can never match a real lease (epochs start at 1) -> DENY-ALL.
_DENY_EPOCH = 0


def ownership_enabled(settings: Settings | None = None) -> bool:
    """True when the ownership gate should be wired into the live path.

    Precedence: an explicit ``Settings.ownership_enabled`` wins when provided,
    otherwise the ``ANTIGONA_OWNERSHIP_ENABLED`` env var.  Default OFF.
    """
    if settings is not None and getattr(settings, "ownership_enabled", None) is not None:
        return bool(settings.ownership_enabled)
    return os.getenv(ENV_ENABLED, "0") in ("1", "true", "True")


def default_ownership_dir(settings: Settings | None = None) -> Path:
    """Shared directory under which per-repo ledgers live."""
    if settings is not None:
        cfg_dir = getattr(settings, "ownership_dir", None)
        if cfg_dir:
            return Path(cfg_dir)
    env = os.getenv(ENV_OWNERSHIP_DIR)
    if env:
        return Path(env)
    # Governed runtime resolver: ANTIGONA_STATE_ROOT/ownership in a hardened
    # deployment, dev/test default otherwise; never the read-only code root.
    return paths.ownership_db_dir()


def default_owner_id() -> str:
    """Stable identity for the agent/worker performing protected writes."""
    wid = os.getenv(ENV_WORKER_ID)
    if wid:
        return wid
    return f"agent-{socket.gethostname()}"


def shared_ledger_path(
    repo_uuid: str, settings: Settings | None = None, ownership_dir: Path | str | None = None
) -> Path:
    """Stable SHARED ledger path for a logical repo (one authority per repo).

    NOT a per-task path: keyed on ``repo_uuid`` under a shared ownership dir, so
    two clones/tasks/processes of the same repo converge on one durable ledger and
    therefore one central authority (INV-01 / INV-08).
    """
    base = Path(ownership_dir) if ownership_dir else default_ownership_dir(settings)
    return base / f"{repo_uuid}.db"


def reconcile_repo(
    repo_root: Path | str,
    settings: Settings | None = None,
    ownership_dir: Path | str | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Dead-owner recovery for a repo (INV-07/FR-3).  Returns the ledger path used, or None.

    ``reconcile`` frees an EXPIRED lease back to FREE so a new owner may acquire
    with a NEW epoch.  Wiring the periodic reconciler: callers may run this on a
    tick; the workspace-setup boundary already reconciles before every acquire.
    """
    identity = resolve_repo_identity(Path(repo_root))
    ledger_path = shared_ledger_path(identity.repo_uuid, settings, ownership_dir)
    ledger = EpochLedger(ledger_path)
    try:
        ledger.reconcile(identity.repo_uuid, now=now)
    finally:
        ledger.close()
    return ledger_path


def _deny_all_context(
    ledger: EpochLedger,
    authority: CentralAuthority,
    repo_uuid: str,
    owner_id: str,
) -> OwnershipContext:
    """A fence token that is guaranteed to DENY every protected write (INV-06).

    Epoch 0 never equals a real epoch (acquires start at 1), and the token never
    matches the live holder, so ``acquire_write_permit`` is always DENIED before
    any filesystem mutation — a read-only/deny-all ownership state.
    """
    return OwnershipContext(
        ledger=ledger,
        repo_uuid=repo_uuid,
        owner_id=owner_id,
        fencing_epoch=_DENY_EPOCH,
        central=authority,
    )



def _unfenced_deny(surface: str) -> FenceCheck:
    """A DENIED fence check for an ownership-enabled write that has no token.

    Used to FAIL CLOSED (INV-06): when ownership is enabled, a protected write
    through a surface that carries no fencing token must be refused before any
    filesystem mutation — never silently allowed unfenced.
    """
    return FenceCheck(
        False,
        f"ownership enabled but write surface {surface!r} has no fencing token; "
        "denying before any mutation (fail-closed)",
        FenceStatus.DENIED_STALE_FENCE,
    )


def mint_execution_ownership(
    repo_root: Path | str,
    owner_id: str | None = None,
    settings: Settings | None = None,
    ownership_dir: Path | str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> OwnershipContext | None:
    """Mint a per-execution fencing token for a write/execute surface (DF-WO2-003-full).

    The worker path binds ownership onto a real ``LocalWorkspace`` object and
    forwards the same token onto its tools.  Live surfaces that carry NO
    workspace object — the model/dialogue ``sandbox.shell`` tool and the
    ``registry.write_file`` handler — had no token at all, so an
    ownership-enabled deployment denied them fail-closed ("has no fencing
    token").  This helper mints the SAME kind of token from the canonical
    authority for those surfaces, keyed on the logical repo identity of
    ``repo_root`` (so every surface of one repo serializes through one ledger).

    Contract (fail-closed, INV-06):

    * ownership DISABLED (default) -> returns ``None``; callers keep their
      existing (unfenced) behaviour byte-for-byte.
    * ownership ENABLED -> returns a LIVE ``OwnershipContext``, or, when the
      repo is already held by another live owner / the authority is
      unavailable, a DENY-ALL context (epoch 0) so the very next fence check
      denies BEFORE any mutation.  It never returns ``None`` while ownership is
      enabled, and it never silently auto-approves.

    The caller MUST bind the token to exactly ONE action and drop it afterwards
    (one-shot per action): the token is a short-lived lease, not a reusable
    capability.
    """
    if not ownership_enabled(settings):
        return None
    owner = owner_id or default_owner_id()
    identity = resolve_repo_identity(Path(repo_root))
    repo_uuid = identity.repo_uuid
    ledger_path = shared_ledger_path(repo_uuid, settings, ownership_dir)
    ledger = EpochLedger(ledger_path)
    authority = CentralAuthority(ledger)
    try:
        # Dead-owner recovery first (INV-07): free any expired lease so this
        # owner may acquire with a NEW epoch.
        authority.reconcile(repo_uuid, now=now)
        acq = authority.acquire(repo_uuid, owner, lease_seconds=lease_seconds, now=now)
        logger.info(
            "ownership minted for execution surface: repo=%s owner=%s epoch=%d ledger=%s",
            repo_uuid,
            owner,
            acq.fencing_epoch,
            ledger_path,
        )
        return OwnershipContext(
            ledger=ledger,
            repo_uuid=repo_uuid,
            owner_id=owner,
            fencing_epoch=acq.fencing_epoch,
            correlation_id=None,
            central=authority,
        )
    except OwnershipBusyError:
        logger.warning(
            "ownership busy for repo %s (held by another live owner); "
            "issuing DENY-ALL token (fail-closed, INV-06)",
            repo_uuid,
        )
        return _deny_all_context(ledger, authority, repo_uuid, owner)
    except Exception as exc:  # noqa: BLE001 — fail closed on any authority unavailability
        logger.warning(
            "ownership authority unavailable for repo %s (%s); "
            "issuing DENY-ALL token (fail-closed, INV-06)",
            repo_uuid,
            exc,
        )
        return _deny_all_context(ledger, authority, repo_uuid, owner)


def enforce_write_fence(
    ownership: OwnershipContext | None,
    surface: str,
    settings: Settings | None = None,
) -> WritePermit | None:
    """Enforce the ownership write fence at a mutation boundary (DF-WO2-003-full).

    Single choke point shared by EVERY live write/execute surface
    (``LocalWorkspace``, ``WorkspaceFileTool``/``DockerSandboxBackend``,
    ``DockerShellTool``, the WRITE_FILE registry handler, and the
    Docker/SSH/Modal/Daytona backends):

    * ownership DISABLED (default) -> returns ``None`` and does nothing, so
      behaviour is byte-for-byte unchanged (backward-compat).
    * ownership ENABLED but ``ownership`` is ``None`` (this surface carries no
      fencing token) -> raises ``FenceDeniedError`` BEFORE any mutation
      (fail-closed, INV-06): an unfenced write is never silently allowed.
    * ownership ENABLED and a token is bound -> acquires an ATOMIC write permit
      from the central authority (DENY on stale epoch / wrong owner / expired
      lease, also before any mutation).

    Returns a ``WritePermit`` to hold across the mutation (TOCTOU closure,
    DF-WO2-002); the caller releases it (a time-bounded hold) after the side
    effect.
    """
    if ownership is None:
        if ownership_enabled(settings):
            raise FenceDeniedError(_unfenced_deny(surface))
        return None
    return ownership.acquire_write_permit()


def release_workspace_ownership(ownership: OwnershipContext | None) -> bool:
    """Release and close a task-scoped ownership context after one worker claim."""
    if ownership is None:
        return False
    released = False
    try:
        authority = ownership.central
        if authority is not None and ownership.fencing_epoch != _DENY_EPOCH:
            released = authority.release(ownership.repo_uuid, ownership.owner_id)
        return released
    finally:
        ownership.ledger.close()



def bind_workspace_ownership(
    workspace: Any,
    repo_root: Path | str,
    owner_id: str | None = None,
    settings: Settings | None = None,
    ownership_dir: Path | str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> OwnershipContext | None:
    """Wire a live fencing token onto ``workspace`` (DF-WO2-003).

    Returns the bound ``OwnershipContext``, or ``None`` when ownership is disabled
    (backward-compat: behaviour unchanged) or the workspace backend does not
    support ownership (non-``LocalWorkspace``).  When enabled but the repo is
    already held by another live owner (or the authority is unavailable), the
    workspace is bound to a DENY-ALL context so protected writes FAIL CLOSED with
    no side effect — never an unfenced no-op (INV-06).
    """
    if not ownership_enabled(settings):
        return None
    bind = getattr(workspace, "bind_ownership", None)
    if bind is None:
        # Only LocalWorkspace carries the ownership fence; remote/mock backends are
        # not fenced here (documented limitation — the local workspace path is the
        # live-agent fence point).
        logger.debug(
            "ownership enabled but backend %r has no bind_ownership; not fenced",
            type(workspace).__name__,
        )
        return None
    owner = owner_id or default_owner_id()
    identity = resolve_repo_identity(Path(repo_root))
    repo_uuid = identity.repo_uuid
    ledger_path = shared_ledger_path(repo_uuid, settings, ownership_dir)
    ledger = EpochLedger(ledger_path)
    authority = CentralAuthority(ledger)
    try:
        # Dead-owner recovery first (INV-07): free any expired lease so this owner
        # may acquire with a NEW epoch.  Mirrors kernel/dispatcher reconcile-on-tick.
        authority.reconcile(repo_uuid, now=now)
        acq = authority.acquire(repo_uuid, owner, lease_seconds=lease_seconds, now=now)
        ctx = OwnershipContext(
            ledger=ledger,
            repo_uuid=repo_uuid,
            owner_id=owner,
            fencing_epoch=acq.fencing_epoch,
            correlation_id=None,
            central=authority,
        )
        bind(ctx)
        logger.info(
            "ownership wired: repo=%s owner=%s epoch=%d ledger=%s",
            repo_uuid,
            owner,
            acq.fencing_epoch,
            ledger_path,
        )
        return ctx
    except OwnershipBusyError:
        logger.warning(
            "ownership busy for repo %s (held by another live owner); "
            "binding workspace read-only (fail-closed, INV-06)",
            repo_uuid,
        )
        deny = _deny_all_context(ledger, authority, repo_uuid, owner)
        bind(deny)
        return deny
    except Exception as exc:  # noqa: BLE001 — fail closed on any authority unavailability
        logger.warning(
            "ownership authority unavailable for repo %s (%s); "
            "binding workspace read-only (fail-closed, INV-06)",
            repo_uuid,
            exc,
        )
        try:
            deny = _deny_all_context(ledger, authority, repo_uuid, owner)
            bind(deny)
        except Exception:  # pragma: no cover — cannot even bind deny-all; log only
            logger.exception("ownership: could not bind deny-all context for repo %s", repo_uuid)
        return None


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "bind_workspace_ownership",
    "default_owner_id",
    "default_ownership_dir",
    "enforce_write_fence",
    "mint_execution_ownership",
    "ownership_enabled",
    "reconcile_repo",
    "release_workspace_ownership",
    "shared_ledger_path",
]
