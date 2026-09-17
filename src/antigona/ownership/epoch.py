"""INV-03 + INV-04 — Monotonic Fencing Epoch and stale-writer gate.

WORKSPACE OWNERSHIP v2 (wave-wo2-20260821_051217).  Per-repository monotonic
fencing counter plus the fencing primitive that a protected workspace write MUST
pass before it may touch the filesystem.

Contract (see ``02_REQUIREMENTS.md`` INV-03 / FR-2 / INV-04 / FR-6):

* **INV-03 — MONOTONIC FENCING.**  Every ``acquire``/``takeover`` issues
  ``fencing_epoch := stored_epoch + 1``.  The epoch strictly increases over the
  repository lifetime and is never reused or decreased.  ``renew``/``release``
  keep the epoch.
* **INV-04 — STALE WRITER MUST FAIL.**  ``verify_before_write`` compares the
  caller's presented ``(owner_id, fencing_epoch)`` against the durable current
  lease.  A stale epoch or a non-matching owner is rejected
  (``FenceCheck.allowed == False``, ``DENIED_STALE_FENCE``) BEFORE any
  filesystem mutation, so the rejected write has provably no side effect.
* **Durability (INV-07) / fail-closed (INV-06).**  The epoch + lease live in a
  standalone SQLite ledger (``<workspace>/.antigona-ownership/ownership.db``)
  keyed on ``repo_uuid``, so they survive a restart of any component.  Any
  ambiguity (no lease record, expired lease, non-HELD state) refuses the write.
* **PHASE 4 — Lease hardening (INV-05/INV-07).**  ``renew`` extends ``expires_at``
  without changing owner or epoch; ``is_expired`` / ``verify_ownership`` make expiry
  and current-ownership explicit; ``reconcile`` performs dead-owner recovery
  (EXPIRED -> FREE) so a new owner may acquire with a NEW epoch — expiry is never
  revived.  Clock assumption is bounded to a single-authority local clock; cross-host
  clock skew is addressed by the central authority in PHASE 6.
* **Atomic claim (INV-01).**  ``acquire``/``takeover`` is a single conditional
  UPDATE + rowcount in one SQLite statement — exactly one of N concurrent
  callers wins (mirrors ``kernel/store.py`` ``claim_task`` atomic-claim
  pattern, store.py:233-280).

The authoritative epoch source is the central authority (later phases); this
module is the durable LOCAL epoch ledger per ``repo_uuid`` that the authority
and the protected write boundary both consult.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String, create_engine, insert, or_, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .identity import OWNERSHIP_DIR

if TYPE_CHECKING:  # pragma: no cover
    from .central import CentralAuthority

logger = logging.getLogger(__name__)

#: Ledger filename inside the ownership dir.
LEDGER_FILENAME = "ownership.db"

#: Audit table (append-only, INV-09) name.
AUDIT_TABLE = "workspace_ownership_audit"

#: Reserved audit actions (INV-09). ``ACTION_CENTRAL_AUTHORITY_FAILURE`` is reserved for
#: the PHASE 6 central authority; no local layer emits it yet.
ACTION_ACQUIRE = "acquire"
ACTION_TAKEOVER = "takeover"
ACTION_RELEASE = "release"
ACTION_RENEW = "renew"
ACTION_EXPIRY = "expiry"
ACTION_WRITE_CHECK = "write_check"
ACTION_CENTRAL_AUTHORITY_FAILURE = "central_authority_failure"

#: Audit decisions.
DECISION_SUCCESS = "SUCCESS"
DECISION_ALLOWED = "ALLOWED"
DECISION_DENIED_BUSY = "DENIED_BUSY"
DECISION_DENIED_NOT_HOLDER = "DENIED_NOT_HOLDER"
DECISION_DENIED_RENEW = "DENIED_RENEW"
DECISION_RECONCILED_FREE = "RECONCILED_FREE"
DECISION_FAILED = "FAILED"

#: Lease states (ownership-relevant subset of the 8-state machine used by this phase).
#: See 03_STATE_MACHINE.md — RENEWING/RELEASING are modeled as atomic transition labels,
#: BLOCKED as ``OwnershipBusyError``/fail-closed denies, ERROR as raised exceptions, and
#: FENCED as the derived outcome of epoch-staleness in ``verify_before_write``
#: (``DENIED_STALE_FENCE``) rather than a stored row at this local layer.
_STATE_FREE = "FREE"
_STATE_HELD = "HELD"
_STATE_EXPIRED = "EXPIRED"


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalize a stored datetime to UTC-aware.

    SQLite persists ``DateTime(timezone=True)`` WITHOUT the offset (it stores
    the naive format string and returns it naive on read). Comparisons against
    ``_now()`` must therefore be done in aware space.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _rowcount(result: object) -> int:
    """Rowcount of a ``session.execute(update)`` CursorResult (mypy stub gap)."""
    return int(getattr(result, "rowcount", 0) or 0)


def default_ledger_path(workspace_root: Path | str) -> Path:
    """Durable per-repo epoch ledger path inside the workspace ownership dir."""
    return Path(workspace_root).resolve() / OWNERSHIP_DIR / LEDGER_FILENAME


class _Base(DeclarativeBase):
    """Standalone declarative base for the ownership ledger (not the app Base)."""


class WorkspaceEpochRow(_Base):
    """One durable row per repository: the monotonic epoch + the current lease."""

    __tablename__ = "workspace_ownership_epoch"

    repo_uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default=_STATE_FREE)


class WorkspaceAuditRow(_Base):
    """One immutable, append-only audit entry (INV-09).

    Exactly the contract shape:
    ``event_id/timestamp/repo_uuid/owner_id/lease_id/fencing_epoch/action/decision/reason/correlation_id``.
    Entries are only ever INSERTed; UPDATE/DELETE are blocked by SQLite triggers
    installed in :meth:`EpochLedger._install_append_only_guard`. ``event_id`` is a
    monotonic autoincrement, so ordering by ``event_id`` equals chronology.
    """

    __tablename__ = AUDIT_TABLE

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    repo_uuid: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fencing_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String)  # TEXT
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)


class FenceStatus(enum.Enum):
    """Typed outcome of a fencing check."""

    ALLOWED = "allowed"
    DENIED_STALE_FENCE = "denied_stale_fence"
    DENIED_NO_LEASE = "denied_no_lease"
    DENIED_EXPIRED = "denied_expired"


@dataclass(frozen=True)
class FenceCheck:
    """Result of ``verify_before_write``. ``allowed == False`` blocks the write."""

    allowed: bool
    reason: str
    status: FenceStatus


@dataclass(frozen=True)
class OwnershipCheck:
    """Read-only confirmation of who currently holds the repo (distinct from the write fence).

    ``verify_ownership`` answers "am I the current holder at the current epoch with a live
    lease?" — an informative check.  It is NOT the enforcement boundary; a protected write
    MUST still pass ``verify_before_write`` (INV-04).  ``is_holder`` is True only when the
    presented ``(owner_id, fencing_epoch)`` matches the current live holder+epoch and the
    lease is not expired.
    """

    is_holder: bool
    is_expired: bool
    current_holder: str | None
    current_epoch: int
    state: str
    reason: str


@dataclass(frozen=True)
class AcquireResult:
    """Result of a successful ``acquire``/``takeover`` (exact contract shape)."""

    repo_uuid: str
    owner_id: str
    lease_id: str
    fencing_epoch: int
    expires_at: datetime


class OwnershipBusyError(Exception):
    """``acquire``/``takeover`` denied: a live lease is already held for the repo."""


class FenceDeniedError(Exception):
    """Protected write fenced (stale epoch / wrong owner). No side effect occurred."""

    def __init__(self, check: FenceCheck) -> None:
        self.check = check
        super().__init__(f"protected write denied: {check.reason}")


@dataclass(frozen=True)
class WritePermit:
    """A single, time-bounded write permit issued by the central authority.

    PHASE 6 (DF-WO2-002 TOCTOU closure).  ``acquire_write_permit`` performs the
    ATOMIC epoch+owner+lease-validity check AND HOLD: on success it extends the
    lease to ``valid_until`` (so a concurrent takeover, which needs FREE/EXPIRED,
    CANNOT win during the write window).  Because the authority holds the fence
    across the mutation, ownership cannot change between the check and the side
    effect -- the verify->write TOCTOU gap is closed (INV-04 / FR-4 / FR-6).
    Permits are bounded by ``valid_until``; they are single-use conceptual tokens
    that the mutation boundary spends by performing the write while the hold is
    live.
    """

    permit_id: str
    repo_uuid: str
    owner_id: str
    fencing_epoch: int
    valid_until: datetime


@dataclass(frozen=True)
class AuditEvent:
    """One immutable audit-trail entry in the exact INV-09 contract shape."""

    event_id: int
    timestamp: datetime
    repo_uuid: str
    owner_id: str | None
    lease_id: str | None
    fencing_epoch: int | None
    action: str
    decision: str
    reason: str
    correlation_id: str | None


class EpochLedger:
    """Durable, atomic, per-``repo_uuid`` monotonic fencing epoch + lease store.

    PHASE 5 (INV-09): every ownership mutation and every write-fence check is
    additionally recorded to the append-only :class:`WorkspaceAuditRow` table in
    the SAME SQLite ledger, so the full chronology is durably reconstructable.
    """

    def __init__(self, ledger_path: Path | str) -> None:
        self._path = Path(ledger_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{self._path}"
        self._engine: Engine = create_engine(url, connect_args={"check_same_thread": False})
        _Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)
        self._install_append_only_guard()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── audit trail (INV-09) ────────────────────────────────────────────────

    def _install_append_only_guard(self) -> None:
        """Install SQLite triggers that make the audit table append-only.

        Any UPDATE or DELETE on the audit table is aborted at the DB layer, so a
        buggy caller cannot silently rewrite or erase history. INSERTs only.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS workspace_ownership_audit_no_update "
                    f"BEFORE UPDATE ON {AUDIT_TABLE} "
                    "BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS workspace_ownership_audit_no_delete "
                    f"BEFORE DELETE ON {AUDIT_TABLE} "
                    "BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END"
                )
            )

    def new_correlation_id(self) -> str:
        """Generate a fresh ``correlation_id`` for a logical operation."""
        return str(uuid.uuid4())

    def record_central_authority_failure(
        self,
        repo_uuid: str,
        owner_id: str | None = None,
        correlation_id: str | None = None,
        reason: str = "ownership authority unavailable",
    ) -> int:
        """Emit the reserved ``central_authority_failure`` audit event (INV-09 / INV-06).

        Used when the central authority fails closed (INV-06) — authority
        unreachable / ambiguous / verification failure.  The event is appended in
        its own short session so it is recorded even when the triggering operation
        raised.  Returns the new ``event_id``.
        """
        return self._append_audit(
            repo_uuid,
            ACTION_CENTRAL_AUTHORITY_FAILURE,
            DECISION_FAILED,
            reason,
            owner_id=owner_id,
            correlation_id=correlation_id,
        )

    def _append_audit(
        self,
        repo_uuid: str,
        action: str,
        decision: str,
        reason: str,
        *,
        owner_id: str | None = None,
        lease_id: str | None = None,
        fencing_epoch: int | None = None,
        correlation_id: str | None = None,
    ) -> int:
        """Insert one immutable audit row (append-only) and commit it durably.

        Runs in its own short session so the entry is persisted even when the
        triggering mutation raises (e.g. ``OwnershipBusyError``) or returns False.
        ``correlation_id`` defaults to a fresh id when the caller does not supply
        one (backward compatible). Returns the new ``event_id``.
        """
        corr = correlation_id or str(uuid.uuid4())
        with self._session() as session:
            row = WorkspaceAuditRow(
                timestamp=_now(),
                repo_uuid=repo_uuid,
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_epoch=fencing_epoch,
                action=action,
                decision=decision,
                reason=reason,
                correlation_id=corr,
            )
            session.add(row)
            session.flush()
            event_id = int(row.event_id)
        return event_id

    def audit_history(self, repo_uuid: str, limit: int | None = None) -> list[AuditEvent]:
        """Chronological reconstruction of every audit event for ``repo_uuid``.

        Ordered by ``event_id`` (monotonic autoincrement == insertion order ==
        wall-clock order). ``limit`` optionally truncates to the most-recent N.
        This is the helper that satisfies T13 (audit reconstruction).
        """
        with self._session() as session:
            query = (
                session.query(WorkspaceAuditRow)
                .filter(WorkspaceAuditRow.repo_uuid == repo_uuid)
                .order_by(WorkspaceAuditRow.event_id)
            )
            if limit is not None:
                query = query.limit(limit)
            rows = list(query.all())
            return [
                AuditEvent(
                    event_id=int(r.event_id),
                    timestamp=_as_aware(r.timestamp) or _now(),
                    repo_uuid=r.repo_uuid,
                    owner_id=r.owner_id,
                    lease_id=r.lease_id,
                    fencing_epoch=r.fencing_epoch,
                    action=r.action,
                    decision=r.decision,
                    reason=r.reason,
                    correlation_id=r.correlation_id,
                )
                for r in rows
            ]

    # ── ownership claim (atomic) ────────────────────────────────────────────

    def acquire(
        self,
        repo_uuid: str,
        owner_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AcquireResult:
        """Atomically claim the repo with a NEW monotonic epoch.

        Exactly one of N concurrent callers wins: a single conditional UPDATE +
        rowcount (only succeeds when no live lease is held), bumping the stored
        epoch by one.  A caller against a live lease raises ``OwnershipBusyError``
        (never an in-place overwrite of a HELD lease — INV-01/INV-03).  Records an
        ``acquire`` audit event (SUCCESS or DENIED_BUSY, INV-09).
        """
        return self._claim(repo_uuid, owner_id, lease_seconds, now, correlation_id, ACTION_ACQUIRE)

    def takeover(
        self,
        repo_uuid: str,
        owner_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AcquireResult:
        """Safe takeover: supersede an EXPIRED/FREE lease with a NEW epoch.

        Takeover never overwrites a live HELD lease — it uses the same atomic
        conditional claim as ``acquire`` (the old epoch is superseded, the new
        holder's fence is strictly newer, INV-03 / INV-04).  Records a ``takeover``
        audit event (SUCCESS or DENIED_BUSY, INV-09).
        """
        return self._claim(repo_uuid, owner_id, lease_seconds, now, correlation_id, ACTION_TAKEOVER)

    def _claim(
        self,
        repo_uuid: str,
        owner_id: str,
        lease_seconds: int,
        now: datetime | None,
        correlation_id: str | None,
        action: str,
    ) -> AcquireResult:
        """Shared atomic claim engine for ``acquire``/``takeover`` + audit."""
        ts = now or _now()
        expiry = ts + timedelta(seconds=lease_seconds)
        lease_id = str(uuid.uuid4())
        epoch = 0
        won = False
        with self._session() as session:
            # Bootstrap an idempotent epoch-0 FREE row if none exists yet.
            session.execute(
                insert(WorkspaceEpochRow)
                .values(repo_uuid=repo_uuid, fencing_epoch=0, state=_STATE_FREE)
                .prefix_with("OR IGNORE")  # SQLite: no-op if the row already exists
            )
            res = session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    or_(
                        WorkspaceEpochRow.state == _STATE_FREE,
                        WorkspaceEpochRow.expires_at.is_(None),
                        WorkspaceEpochRow.expires_at < ts,
                    ),
                )
                .values(
                    owner_id=owner_id,
                    fencing_epoch=WorkspaceEpochRow.fencing_epoch + 1,
                    lease_id=lease_id,
                    issued_at=ts,
                    expires_at=expiry,
                    state=_STATE_HELD,
                )
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) == 1:
                won = True
                row = session.get(WorkspaceEpochRow, repo_uuid)
                epoch = int(row.fencing_epoch) if row is not None else 0
        if won:
            self._append_audit(
                repo_uuid,
                action,
                DECISION_SUCCESS,
                f"{action} succeeded: holder={owner_id!r} at epoch {epoch}",
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_epoch=epoch,
                correlation_id=correlation_id,
            )
            return AcquireResult(
                repo_uuid=repo_uuid,
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_epoch=epoch,
                expires_at=expiry,
            )
        # Busy (fail-closed): audit the denial BEFORE raising, using the current epoch.
        self._append_audit(
            repo_uuid,
            action,
            DECISION_DENIED_BUSY,
            f"repo {repo_uuid} already held by a live lease; cannot {action}",
            owner_id=owner_id,
            fencing_epoch=self.current_epoch(repo_uuid),
            correlation_id=correlation_id,
        )
        raise OwnershipBusyError(f"repo {repo_uuid} already held by a live lease; cannot acquire")

    def release(
        self,
        repo_uuid: str,
        owner_id: str,
        correlation_id: str | None = None,
    ) -> bool:
        """Release a HELD lease back to FREE. Only the holder may release.

        The epoch is preserved so the next acquire issues a strictly higher
        epoch (INV-03).  Records a ``release`` audit event (SUCCESS or
        DENIED_NOT_HOLDER, INV-09).
        """
        success = False
        lease_id: str | None = None
        epoch = 0
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is not None:
                lease_id = row.lease_id
                epoch = int(row.fencing_epoch)
            res = session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    WorkspaceEpochRow.owner_id == owner_id,
                    WorkspaceEpochRow.state == _STATE_HELD,
                )
                .values(
                    owner_id=None,
                    lease_id=None,
                    issued_at=None,
                    expires_at=None,
                    state=_STATE_FREE,
                )
                .execution_options(synchronize_session=False)
            )
            success = _rowcount(res) == 1
        decision = DECISION_SUCCESS if success else DECISION_DENIED_NOT_HOLDER
        reason = (
            f"lease released by holder {owner_id!r}"
            if success
            else f"release denied: {owner_id!r} is not the current holder"
        )
        self._append_audit(
            repo_uuid,
            ACTION_RELEASE,
            decision,
            reason,
            owner_id=owner_id,
            lease_id=lease_id,
            fencing_epoch=epoch,
            correlation_id=correlation_id,
        )
        return success

    # ── fencing check (INV-04) ─────────────────────────────────────────────

    def verify_before_write(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> FenceCheck:
        """Fencing primitive a protected write MUST pass before mutating state.

        Denied (fail-closed) on any of: no lease record, non-HELD state,
        expired lease, owner mismatch, or stale/presented epoch != current.
        Every check (allowed AND denied) is recorded as a ``write_check`` audit
        event with decision ALLOWED / DENIED_* (INV-09).
        """
        ts = now or _now()
        lease_id: str | None = None
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is None:
                check = FenceCheck(
                    False,
                    f"no lease record for repo {repo_uuid}; nothing held",
                    FenceStatus.DENIED_NO_LEASE,
                )
            elif row.state != _STATE_HELD:
                check = FenceCheck(
                    False,
                    f"repo {repo_uuid} not held (state={row.state})",
                    FenceStatus.DENIED_NO_LEASE,
                )
            else:
                stored_expiry = _as_aware(row.expires_at)
                if stored_expiry is None or stored_expiry < ts:
                    # Fail-closed on clock ambiguity (INV-06): a HELD lease with no explicit
                    # expires_at violates INV-05 (explicit lifetime) and is treated as expired.
                    reason = (
                        "lease expired"
                        if stored_expiry is not None
                        else "lease has no explicit expiry; ambiguous (fail-closed)"
                    )
                    check = FenceCheck(False, reason, FenceStatus.DENIED_EXPIRED)
                elif row.owner_id != owner_id:
                    check = FenceCheck(
                        False,
                        f"owner mismatch: presented {owner_id!r}, holder {row.owner_id!r}",
                        FenceStatus.DENIED_STALE_FENCE,
                    )
                else:
                    current = int(row.fencing_epoch)
                    if fencing_epoch != current:
                        check = FenceCheck(
                            False,
                            f"stale fencing epoch: presented {fencing_epoch}, current {current}",
                            FenceStatus.DENIED_STALE_FENCE,
                        )
                    else:
                        check = FenceCheck(True, "allowed", FenceStatus.ALLOWED)
            if row is not None:
                lease_id = row.lease_id
        # Uppercase decisions matching DECISION_* constants: "ALLOWED" and the
        # FenceStatus names (DENIED_NO_LEASE / DENIED_STALE_FENCE / DENIED_EXPIRED).
        decision = check.status.name
        self._append_audit(
            repo_uuid,
            ACTION_WRITE_CHECK,
            decision,
            check.reason,
            owner_id=owner_id,
            lease_id=lease_id,
            fencing_epoch=fencing_epoch,
            correlation_id=correlation_id,
        )
        return check

    def current_epoch(self, repo_uuid: str) -> int:
        """Return the durable current epoch for the repo (0 if never acquired)."""
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            return int(row.fencing_epoch) if row is not None else 0

    # ── lease lifecycle (INV-05) ───────────────────────────────────────────

    def renew(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        extra_seconds: int,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Extend the lease's ``expires_at`` by ``extra_seconds``.

        Conditional-atomic (single UPDATE + rowcount, mirrors ``kernel/store.py``
        ``renew_lease`` store.py:282-333).  Only the CURRENT holder at the CURRENT
        epoch with a live (unexpired) HELD lease may renew; the fencing epoch and the
        owner are UNCHANGED (INV-05 / INV-03 / FR-3 T3->T4).  Denied (returns False,
        fail-closed) for: wrong owner, stale epoch, expired lease (EXPIRED->RENEWING
        is FORBIDDEN — expiry is never revived, INV-07), non-HELD state, or unknown repo.
        """
        ts = now or _now()
        new_expiry = ts + timedelta(seconds=extra_seconds)
        success = False
        lease_id: str | None = None
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is not None:
                lease_id = row.lease_id
            res = session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    WorkspaceEpochRow.owner_id == owner_id,
                    WorkspaceEpochRow.fencing_epoch == fencing_epoch,
                    WorkspaceEpochRow.state == _STATE_HELD,
                    or_(
                        WorkspaceEpochRow.expires_at.is_(None),
                        WorkspaceEpochRow.expires_at > ts,
                    ),
                )
                .values(expires_at=new_expiry)
                .execution_options(synchronize_session=False)
            )
            success = _rowcount(res) == 1
        decision = DECISION_SUCCESS if success else DECISION_DENIED_RENEW
        reason = (
            f"lease extended to {new_expiry.isoformat()}"
            if success
            else f"renew denied for {owner_id!r}@epoch {fencing_epoch}"
        )
        self._append_audit(
            repo_uuid,
            ACTION_RENEW,
            decision,
            reason,
            owner_id=owner_id,
            lease_id=lease_id,
            fencing_epoch=fencing_epoch,
            correlation_id=correlation_id,
        )
        return success

    def acquire_write_permit(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        permit_seconds: int = 60,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> WritePermit:
        """Atomically validate epoch+owner+lease AND hold the fence across a write.

        PHASE 6 (DF-WO2-002 TOCTOU closure).  This is the central-authority's
        ATOMIC epoch+owner+lease validity check collapsed into the protected-write
        boundary.  A SINGLE conditional UPDATE succeeds only when the caller is the
        CURRENT holder at the CURRENT epoch with a live (unexpired) HELD lease; on
        success it extends ``expires_at`` to ``now + permit_seconds`` -- HOLDING the
        lease so a concurrent takeover (which needs FREE/EXPIRED) cannot win during
        the write window.  Ownership therefore cannot change between the check and
        the mutation: the verify->write TOCTOU gap is closed (INV-04 / FR-4 / FR-6).

        Fail-closed (INV-06): no lease / non-HELD / expired / wrong owner / stale
        epoch => 0 matched rows => raises ``FenceDeniedError`` BEFORE any filesystem
        mutation (provably no side effect, INV-04).  Audited as a ``write_check``
        event (ALLOWED / DENIED_*, INV-09).
        """
        ts = now or _now()
        permit_id = str(uuid.uuid4())
        valid_until = ts + timedelta(seconds=permit_seconds)
        won = False
        lease_id: str | None = None
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is not None:
                lease_id = row.lease_id
            res = session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    WorkspaceEpochRow.owner_id == owner_id,
                    WorkspaceEpochRow.fencing_epoch == fencing_epoch,
                    WorkspaceEpochRow.state == _STATE_HELD,
                    or_(
                        WorkspaceEpochRow.expires_at.is_(None),
                        WorkspaceEpochRow.expires_at > ts,
                    ),
                )
                .values(expires_at=valid_until)
                .execution_options(synchronize_session=False)
            )
            won = _rowcount(res) == 1
        if won:
            self._append_audit(
                repo_uuid,
                ACTION_WRITE_CHECK,
                DECISION_ALLOWED,
                f"write permit granted to {owner_id!r}@epoch {fencing_epoch}, "
                f"fence held to {valid_until.isoformat()}",
                owner_id=owner_id,
                lease_id=lease_id,
                fencing_epoch=fencing_epoch,
                correlation_id=correlation_id,
            )
            return WritePermit(
                permit_id=permit_id,
                repo_uuid=repo_uuid,
                owner_id=owner_id,
                fencing_epoch=fencing_epoch,
                valid_until=valid_until,
            )
        # Denied: recover the precise reason (also records the DENIED write_check,
        # INV-09). The enforcement was the atomic 0-row UPDATE.
        try:
            check = self.verify_before_write(
                repo_uuid, owner_id, fencing_epoch, now=ts, correlation_id=correlation_id
            )
        except Exception:
            check = FenceCheck(
                False,
                f"write permit denied for {owner_id!r}@epoch {fencing_epoch}",
                FenceStatus.DENIED_STALE_FENCE,
            )
        raise FenceDeniedError(check)

    def is_expired(self, repo_uuid: str, now: datetime | None = None) -> bool:
        """True when the repo's lease has passed ``expires_at`` (or is otherwise not live).

        An expired lease is NOT ownership (INV-05); ``verify_before_write`` denies it
        and ``reconcile`` frees it for a new owner.  A HELD record with no explicit
        ``expires_at`` violates INV-05 and is treated as expired (fail-closed, INV-06).
        A FREE / never-acquired repo is not "expired" (nothing is held).
        """
        return self.state_of(repo_uuid, now=now) == _STATE_EXPIRED

    def state_of(self, repo_uuid: str, now: datetime | None = None) -> str:
        """Effective ownership state for the repo: one of FREE / HELD / EXPIRED.

        EXPIRED is DERIVED from the lease clock (HELD with ``expires_at < now``), so the
        local layer exposes it without a separate persisted transition; the stored
        ``state`` column still tracks FREE/HELD.  This maps the contract's
        FREE/HELD/EXPIRED subset of the 8-state machine (03_STATE_MACHINE.md).
        """
        ts = now or _now()
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is None or row.state == _STATE_FREE:
                return _STATE_FREE
            if row.state == _STATE_EXPIRED:
                return _STATE_EXPIRED
            if row.state == _STATE_HELD:
                expiry = _as_aware(row.expires_at)
                if expiry is None or expiry < ts:
                    return _STATE_EXPIRED
                return _STATE_HELD
            return row.state

    def verify_ownership(
        self,
        repo_uuid: str,
        owner_id: str,
        fencing_epoch: int,
        now: datetime | None = None,
    ) -> OwnershipCheck:
        """Confirm the CURRENT holder + epoch (read-only ownership verification).

        Distinct from ``verify_before_write`` (the write fence): this never blocks or
        mutates, it only reports whether the presented token is the current live holder.
        ``is_holder`` is True iff state==HELD, unexpired, holder matches, epoch matches.
        """
        state = self.state_of(repo_uuid, now=now)
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
        holder: str | None = row.owner_id if row is not None else None
        epoch = int(row.fencing_epoch) if row is not None else 0
        is_expired = state == _STATE_EXPIRED
        is_holder = (
            state == _STATE_HELD
            and holder == owner_id
            and epoch == fencing_epoch
            and not is_expired
        )
        if is_holder:
            reason = "current holder at current epoch with a live lease"
        else:
            reason = (
                f"repo {repo_uuid}: state={state}, holder={holder!r}, current_epoch={epoch}; "
                f"presented owner={owner_id!r}, epoch={fencing_epoch}"
            )
        return OwnershipCheck(
            is_holder=is_holder,
            is_expired=is_expired,
            current_holder=holder,
            current_epoch=epoch,
            state=state,
            reason=reason,
        )

    def current_owner(self, repo_uuid: str) -> str | None:
        """Durable current lease owner for the repo (None if free/never acquired)."""
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            return row.owner_id if row is not None else None

    def lease_expiry(self, repo_uuid: str) -> datetime | None:
        """Durable current lease ``expires_at`` for the repo (None if free/unknown)."""
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            return _as_aware(row.expires_at) if row is not None else None

    def reconcile(
        self,
        repo_uuid: str,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Dead-owner recovery: free an EXPIRED lease back to FREE (INV-05 / INV-07 / FR-3).

        Mirrors ``kernel/store.reconcile`` (store.py:694+) but for a single repo.  When the
        current lease is HELD and has passed ``expires_at``, it is reclaimed EXPIRED->FREE
        (T8->T11) so a new owner may ``acquire`` with a NEW epoch.  The dead owner is never
        revived — the freed record retains the monotonic epoch, so the next acquire issues
        ``epoch + 1`` and the dead owner's old token is fenced.  Returns False (no-op) when
        the lease is live, already FREE, or the repo is unknown.  FENCED recovery (T10/T12)
        is not stored at this local layer: cross-host supersession is expressed by the
        monotonic epoch (a superseded holder is DENIED_STALE_FENCE).  Records an ``expiry``
        audit event when dead-owner recovery frees a lease (INV-09).
        """
        ts = now or _now()
        freed = False
        dead_owner: str | None = None
        dead_lease: str | None = None
        dead_epoch = 0
        with self._session() as session:
            row = session.get(WorkspaceEpochRow, repo_uuid)
            if row is not None:
                dead_owner = row.owner_id
                dead_lease = row.lease_id
                dead_epoch = int(row.fencing_epoch)
            # T8: mark a HELD lease whose expiry has passed as EXPIRED.
            res = session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    WorkspaceEpochRow.state == _STATE_HELD,
                    or_(
                        WorkspaceEpochRow.expires_at.is_(None),
                        WorkspaceEpochRow.expires_at < ts,
                    ),
                )
                .values(state=_STATE_EXPIRED)
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                return False  # nothing expired to recover
            # T11: clear the dead lease back to FREE (owner/lease/expiry dropped; epoch kept).
            session.execute(
                update(WorkspaceEpochRow)
                .where(
                    WorkspaceEpochRow.repo_uuid == repo_uuid,
                    WorkspaceEpochRow.state == _STATE_EXPIRED,
                )
                .values(
                    owner_id=None,
                    lease_id=None,
                    issued_at=None,
                    expires_at=None,
                    state=_STATE_FREE,
                )
                .execution_options(synchronize_session=False)
            )
            freed = True
        if freed:
            self._append_audit(
                repo_uuid,
                ACTION_EXPIRY,
                DECISION_RECONCILED_FREE,
                f"dead-owner recovery: expired lease of {dead_owner!r} freed back to FREE",
                owner_id=dead_owner,
                lease_id=dead_lease,
                fencing_epoch=dead_epoch,
                correlation_id=correlation_id,
            )
        return True

    def close(self) -> None:
        self._engine.dispose()


@dataclass(frozen=True)
class OwnershipContext:
    """A caller's live fencing token, bound to a workspace write path.

    Holds the durable ledger plus the caller's ``(repo_uuid, owner_id,
    fencing_epoch)``.  ``assert_can_write`` is the enforcement boundary: it
    raises ``FenceDeniedError`` on any denial, so the write path can check it
    BEFORE any filesystem mutation (INV-04 — provably no side effect).
    """

    ledger: EpochLedger
    repo_uuid: str
    owner_id: str
    fencing_epoch: int
    correlation_id: str | None = None
    central: CentralAuthority | None = None

    def verify_before_write(self, now: datetime | None = None) -> FenceCheck:
        return self.ledger.verify_before_write(
            self.repo_uuid,
            self.owner_id,
            self.fencing_epoch,
            now=now,
            correlation_id=self.correlation_id,
        )

    def assert_can_write(self, now: datetime | None = None) -> None:
        check = self.verify_before_write(now=now)
        if not check.allowed:
            raise FenceDeniedError(check)

    def acquire_write_permit(self, permit_seconds: int = 60) -> WritePermit | None:
        """Obtain an atomic write permit from the central authority at the mutation boundary.

        PHASE 6 (DF-WO2-002): when a ``central`` authority is bound, the write path
        must spend a write permit so the fence is held across the mutation (TOCTOU
        closure).  When no authority is bound (backward compatible), falls back to the
        durable phase-3 ``assert_can_write`` check and returns ``None``.
        """
        if self.central is not None:
            return self.central.acquire_write_permit(
                self.repo_uuid,
                self.owner_id,
                self.fencing_epoch,
                permit_seconds=permit_seconds,
                correlation_id=self.correlation_id,
            )
        self.assert_can_write()
        return None


__all__ = [
    "ACTION_ACQUIRE",
    "ACTION_CENTRAL_AUTHORITY_FAILURE",
    "ACTION_EXPIRY",
    "ACTION_RELEASE",
    "ACTION_RENEW",
    "ACTION_TAKEOVER",
    "ACTION_WRITE_CHECK",
    "AUDIT_TABLE",
    "AcquireResult",
    "AuditEvent",
    "DECISION_ALLOWED",
    "DECISION_DENIED_BUSY",
    "DECISION_DENIED_NOT_HOLDER",
    "DECISION_DENIED_RENEW",
    "DECISION_FAILED",
    "DECISION_RECONCILED_FREE",
    "DECISION_SUCCESS",
    "EpochLedger",
    "FenceCheck",
    "FenceDeniedError",
    "FenceStatus",
    "LEDGER_FILENAME",
    "OwnershipBusyError",
    "OwnershipCheck",
    "OwnershipContext",
    "WorkspaceAuditRow",
    "WorkspaceEpochRow",
    "WritePermit",
    "default_ledger_path",
]
