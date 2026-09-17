"""PHASE 6 — Central Authority (INV-01 / INV-06 / INV-08) + TOCTOU closure (DF-WO2-002).

WORKSPACE OWNERSHIP v2 (wave-wo2-20260821_051217).  The central authority is the
SINGLE source of truth for ``repo_uuid`` epoch + owner + lease.  Every ownership
mutation (acquire / takeover / renew / release / reconcile), every write-fence
check, and every WRITE-PERMIT go through it, serialized by the durable SQLite
ledger (cross-process safe).  It is the authority the contract's FR-4 describes:
*atomic epoch + owner + lease check*.

The module also CLOSES the phase-3 TOCTOU defect DF-WO2-002.  ``acquire_write_permit``
runs the atomic epoch+owner+lease check AND HOLD in ONE conditional UPDATE at the
ledger: on success it extends the lease so a concurrent takeover (which needs
FREE/EXPIRED) cannot win during the mutation.  Ownership therefore cannot change
between the check and the side effect — the verify->write gap is closed
(INV-04 / FR-6).

Fail-closed semantics (INV-06 / INV-10): any authority unavailability or ambiguity
raises :class:`CentralAuthorityFailure` and the reserved ``central_authority_failure``
audit event is emitted (INV-09).  The caller MUST NOT proceed with an unverified
write.

Design notes (honest):
* Cross-process serialization is the SQLite ledger (all processes share
  ``<workspace>/.antigona-ownership/ownership.db``); within a process the authority
  adds no separate Python lock because the conditional-UPDATE+rowcount pattern is
  already atomic at the DB layer (mirrors ``kernel/store.py`` claim_task).
* A write permit is a time-bounded HOLD.  If the underlying mutation outlives
  ``permit_seconds``, the lease may re-expire and a later takeover could win — but
  each subsequent write re-acquires a fresh permit at its own mutation boundary, so
  a stale writer is still blocked on its next write.  The invariant guaranteed here:
  *between a permit grant and its bounded validity, ownership cannot change*.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .epoch import (
    AcquireResult,
    AuditEvent,
    EpochLedger,
    FenceCheck,
    FenceDeniedError,
    FenceStatus,
    OwnershipBusyError,
    OwnershipCheck,
    WritePermit,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class CentralAuthorityFailure(Exception):
    """Fail-closed (INV-06): the ownership authority is unavailable or ambiguous.

    Raised instead of proceeding with an unverified write.  The caller MUST refuse
    the operation (DENY / BLOCKED, INV-10) and must not touch the filesystem.
    """


class CentralAuthority:
    """The single authority for repo ownership: atomic epoch+owner+lease + write permits.

    Wraps a durable :class:`EpochLedger` (the cross-process serialization point)
    and adds the write-permit mechanism plus fail-closed handling.  Delegates the
    ownership lifecycle to the ledger so there is exactly one writer of epoch state.
    """

    def __init__(self, ledger: EpochLedger) -> None:
        self._ledger = ledger

    # ── ownership lifecycle (delegated to the durable ledger — single writer) ──

    def acquire(
        self,
        repo_uuid: str,
        owner_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AcquireResult:
        return self._ledger.acquire(
            repo_uuid, owner_id, lease_seconds=lease_seconds, now=now, correlation_id=correlation_id
        )

    def takeover(
        self,
        repo_uuid: str,
        owner_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AcquireResult:
        return self._ledger.takeover(
            repo_uuid, owner_id, lease_seconds=lease_seconds, now=now, correlation_id=correlation_id
        )

    def renew(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        extra_seconds: int,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        return self._ledger.renew(
            repo_uuid,
            owner_id,
            fencing_epoch,
            extra_seconds,
            now=now,
            correlation_id=correlation_id,
        )

    def release(self, repo_uuid: str, owner_id: str, correlation_id: str | None = None) -> bool:
        return self._ledger.release(repo_uuid, owner_id, correlation_id=correlation_id)

    def reconcile(
        self,
        repo_uuid: str,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        return self._ledger.reconcile(repo_uuid, now=now, correlation_id=correlation_id)

    # ── read-only authority views ───────────────────────────────────────────

    def verify_before_write(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> FenceCheck:
        """Atomic read of current epoch+owner+lease (the fencing primitive)."""
        return self._ledger.verify_before_write(
            repo_uuid, owner_id, fencing_epoch, now=now, correlation_id=correlation_id
        )

    def verify_ownership(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        now: datetime | None = None,
    ) -> OwnershipCheck:
        return self._ledger.verify_ownership(repo_uuid, owner_id, fencing_epoch, now=now)

    def current_epoch(self, repo_uuid: str) -> int:
        return self._ledger.current_epoch(repo_uuid)

    def current_owner(self, repo_uuid: str) -> str | None:
        return self._ledger.current_owner(repo_uuid)

    def state_of(self, repo_uuid: str, now: datetime | None = None) -> str:
        return self._ledger.state_of(repo_uuid, now=now)

    def is_expired(self, repo_uuid: str, now: datetime | None = None) -> bool:
        return self._ledger.is_expired(repo_uuid, now=now)

    def audit_history(self, repo_uuid: str, limit: int | None = None) -> list[AuditEvent]:
        return self._ledger.audit_history(repo_uuid, limit=limit)

    def new_correlation_id(self) -> str:
        return self._ledger.new_correlation_id()

    # ── write-permit mechanism (TOCTOU closure, DF-WO2-002) ─────────────────

    def acquire_write_permit(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        permit_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> WritePermit:
        """Issue an atomic write permit (check epoch+owner+lease AND hold the fence).

        Fail-closed: a normal denial (stale/expired/wrong-owner) raises
        ``FenceDeniedError`` (not a failure).  An authority/DB error raises
        ``CentralAuthorityFailure`` after emitting the ``central_authority_failure``
        audit event (INV-06 / INV-09).
        """
        try:
            return self._ledger.acquire_write_permit(
                repo_uuid,
                owner_id,
                fencing_epoch,
                permit_seconds=permit_seconds,
                now=now,
                correlation_id=correlation_id,
            )
        except (CentralAuthorityFailure, FenceDeniedError, OwnershipBusyError):
            raise  # expected control flow — not an authority failure
        except Exception as exc:  # authority unavailable -> fail closed (INV-06)
            self._record_authority_failure(
                repo_uuid, owner_id=owner_id, correlation_id=correlation_id, exc=exc
            )
            raise CentralAuthorityFailure(
                f"ownership authority unavailable (fail-closed): {exc}"
            ) from exc

    def release_write_permit(self, permit: WritePermit) -> None:
        """Permits are time-bounded holds; nothing to revoke (kept for API symmetry)."""
        return None

    def check_write_permit(self, permit: WritePermit, now: datetime | None = None) -> FenceCheck:
        """Re-validate an existing permit atomically (belt-and-suspenders).

        True only while the permit is unexpired AND the caller is still the current
        holder at the current epoch with a live lease.  Used if a caller holds a
        permit across several statements and wants to re-verify before the final
        side effect.
        """
        ts = now or _now()
        if ts > permit.valid_until:
            return FenceCheck(
                False,
                f"write permit {permit.permit_id} expired (valid until {permit.valid_until.isoformat()})",
                FenceStatus.DENIED_STALE_FENCE,
            )
        return self._ledger.verify_before_write(
            permit.repo_uuid,
            permit.owner_id,
            permit.fencing_epoch,
            now=ts,
            correlation_id=None,
        )

    # ── fail-closed plumbing (INV-06 / INV-09) ──────────────────────────────

    def _record_authority_failure(
        self,
        repo_uuid: str,
        owner_id: str | None,
        correlation_id: str | None,
        exc: Exception,
    ) -> None:
        """Emit the reserved ``central_authority_failure`` audit event (best-effort)."""
        try:
            self._ledger.record_central_authority_failure(
                repo_uuid,
                owner_id=owner_id,
                correlation_id=correlation_id,
                reason=f"ownership authority unavailable: {exc}",
            )
        except Exception:  # authority is down entirely — log, cannot audit
            logger.exception("could not record central_authority_failure for repo %s", repo_uuid)


__all__ = [
    "CentralAuthority",
    "CentralAuthorityFailure",
    "WritePermit",
]
