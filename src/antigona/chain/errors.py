"""Typed error taxonomy for the content-addressed chain and its lock refusals.

Two *sibling* identities are the deliverable of mechanism M2 (wave G1b):

* :class:`StaleHeadError` — the caller **lost a compare-and-swap**. Authority moved
  under it; a concurrent writer may or may not have committed. Mutation is
  **unknown**, so the caller must re-derive and re-check. It is deliberately a
  subclass of :class:`antigona.durable.state_machine.ConcurrentUpdate` so the
  codebase gains no fifth lost-CAS identity.
* :class:`LockContendedError` — the caller **proved that the guarded body never
  started**: the non-blocking advisory lock was already held. This is strictly
  narrower than a lost CAS and is *not* a
  :class:`~antigona.durable.state_machine.ConcurrentUpdate`.

Neither is a subclass of the other (invariant L1): ``except LockContendedError``
must never swallow a lost compare-and-swap, and vice versa.
"""

from __future__ import annotations

from pathlib import Path

from antigona.durable.state_machine import ConcurrentUpdate

__all__ = [
    "ChainError",
    "ChainIntegrityError",
    "ChainPathError",
    "ChainRecordError",
    "ChainSyncError",
    "InvalidSuccessorError",
    "LegacyReadOnlyError",
    "LockContendedError",
    "LockPreAcquisitionError",
    "StaleHeadError",
]


class ChainError(RuntimeError):
    """Base class for every refusal raised by :mod:`antigona.chain`."""


# ── content / chain integrity — fail closed on corruption ────────────────────


class ChainIntegrityError(ChainError):
    """The stored chain does not match the bytes it claims to be.

    Raised for a missing event, an unparsable or schema-divergent event, a
    non-``sha256:`` predecessor, a predecessor cycle, a discontinuous link, a
    HEAD line that is not a single revision, and for a divergent payload that
    collides on an existing revision. Always fail closed: never "repair",
    never overwrite, never truncate silently.
    """


class ChainRecordError(ChainError):
    """A record is not a legal record (blank operation, non-serialisable payload)."""


class InvalidSuccessorError(ChainError):
    """The domain rules for a genesis or successor record refused the transition.

    The mechanical chain invariants live in this package; the *domain* rules
    (what ``operation`` may follow what) are injected by the caller
    (``validate_genesis`` / ``validate_successor``), so this package stays a
    mechanism rather than a state machine.
    """


class LegacyReadOnlyError(ChainError):
    """The store is bound read-only; a mutating append is refused before any write."""

    def __init__(self, operation: str, lineage_id: str = "") -> None:
        self.operation = operation
        self.lineage_id = lineage_id
        super().__init__(
            f"the chain store is read-only: operation {operation!r} is refused "
            f"for lineage {lineage_id!r}"
        )


# ── filesystem-level refusals ────────────────────────────────────────────────


class ChainPathError(ChainError):
    """A path was refused: a symlink, a hardlink, a non-regular file, or the root.

    The chain never follows links (invariant F3) — an event file that is a
    symlink is not an event file.
    """


class ChainSyncError(ChainError):
    """A durability sync failed. The refusal names the path it failed on (F4).

    A failed sync is never swallowed into success: reporting "durable" without a
    completed ``fsync`` would be exactly the assertion-without-proof the
    Constitution forbids.
    """

    def __init__(self, path: Path | str, operation: str, cause: BaseException) -> None:
        self.path = Path(path)
        self.operation = operation
        self.cause = cause
        super().__init__(f"{operation} failed for {str(self.path)!r}: {cause}")


# ── lost CAS: authority moved under the caller — mutation UNKNOWN ────────────


class StaleHeadError(ChainError, ConcurrentUpdate):
    """CAS loss. A concurrent writer may have committed; re-derive, then re-check.

    NOT a proof of non-mutation: the chain HEAD is no longer the revision the
    caller expected. Carries the three revisions an operator needs — ``expected``
    (what the caller assumed), ``current`` (what HEAD actually is), and
    ``candidate`` (the revision the refused record would have produced).
    """

    def __init__(self, expected: str, current: str, candidate: str) -> None:
        self.expected = expected
        self.current = current
        self.candidate = candidate
        super().__init__(
            f"expected predecessor {expected!r}, current HEAD {current!r}, "
            f"candidate revision {candidate!r}"
        )


# ── lock contention: PROVEN NOT STARTED ─────────────────────────────────────


class LockContendedError(ChainError):
    """The non-blocking advisory lock was already held, so the guarded body never ran.

    Deliberately NARROWER than :class:`StaleHeadError`. Scope of the claim: this
    proves the guarded body never started. It does NOT prove the enclosing
    operation made no side effect — if anything in the same operation committed a
    transition before the lock attempt, that remains genuinely unknown, and a
    caller that needs "nothing at all mutated" must assert that itself (a
    caller-side invariant, not something this package can know).
    Constitutional law 4 ("unknown side-effect state → fail closed") is refined
    here, not repealed.

    The classification comes from the syscall (``flock`` refused with
    ``EAGAIN``/``EWOULDBLOCK``), never from lock-file text: a persisted PID is not
    current-holder proof and grants nothing.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        super().__init__(
            f"the chain store advisory lock at {str(self.path)!r} is held by a live "
            f"holder; the guarded body never ran"
        )


class LockPreAcquisitionError(ChainError):
    """A failure *before* the lock was handed to the caller: directory creation, the
    no-follow open walk, a non-contention ``flock`` error, or persisting the owner
    metadata.

    Not started — but NOT the contention proof: this is an environment or
    configuration defect (``EACCES``/``EROFS``/``ENOENT``/``ENOTDIR``/``ELOOP``/…)
    and must never be reported as a phantom "someone else is busy". Because the
    lock is never handed out, the guarded body still never ran.
    """

    def __init__(self, path: Path | str, cause: BaseException) -> None:
        self.path = Path(path)
        self.cause = cause
        super().__init__(f"the chain store lock at {str(self.path)!r} could not be acquired: {cause}")
