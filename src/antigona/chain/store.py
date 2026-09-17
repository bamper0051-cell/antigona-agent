"""The content-addressed, append-only chain with a HEAD pointer and CAS (wave G1b).

The store is three things and nothing more: a flat ``events/`` directory whose files
are named after their own digest, a single-line ``HEAD`` file naming the current head
revision, and a ``LOCK`` serialising writers. Every append is a compare-and-swap on
``HEAD``; there is no unconditional append.

Ordered commit sequence — the ordering **is** the contract (PLAN.md §4.4, invariants
I1-I10):

1. refuse a read-only store, a blank store directory and a blank operation;
2. seal the record (``schema`` and ``previous_revision = expected_revision``) and
   derive its revision from the canonical bytes the file will hold;
3. create (and sync) ``events/``;
4. take the advisory lock — typed refusal, never "busy" on a setup defect;
5-9. read HEAD; serve an exact retry (full chain validation, never a shortcut);
   refuse a stale predecessor with the three revisions; validate genesis/successor;
10-11. publish the event with no-replace semantics;
12. **sync the events directory before anything may name the event as durable**;
13. write ``HEAD`` atomically.

A crash between 12 and 13 leaves an unreferenced orphan event and an unchanged HEAD
— recoverable. The reverse order is not recoverable and is therefore impossible.

Read-path scope (honest limit): :meth:`ChainStore.load_chain` validates the *linkage*
(schema, JSON shape, ``sha256:`` predecessor shape, cycle, continuity, genesis) and
computes each revision from the bytes it read, so a file whose content does not
reproduce its link is caught as a discontinuity. The head has no child to name it, so
the walk itself compares the requested ``head_revision`` with the revision computed
from the head file's bytes; a swapped head is therefore refused too. Rollback of
``HEAD`` to an older, intact revision is a separate residue that only an anchor
outside the store can catch — see :meth:`ChainStore.load_chain`. It does not re-hash
every ancestor against its filename on each walk: the byte-level guarantee is enforced
at publish time (the name is the digest of the published bytes) and re-checked on the
exact-retry path via :func:`antigona.chain.fsops.read_bytes_verified`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .errors import (
    ChainError,
    ChainIntegrityError,
    ChainRecordError,
    ChainSyncError,
    InvalidSuccessorError,
    LegacyReadOnlyError,
    StaleHeadError,
)
from .fsops import (
    EVENT_TEMP_PREFIX,
    fsync_dir,
    mkdir_all_sync,
    read_bytes_verified,
    read_file_no_follow,
    write_all,
    write_atomic,
    write_immutable_no_replace,
)
from .hashing import chain_identity, is_revision, record_revision
from .locking import StoreLock, acquire_store_lock
from .records import (
    EVENTS_DIRNAME,
    HEAD_FILENAME,
    LOCK_FILENAME,
    RECORD_SCHEMA,
    ChainRecord,
    canonical_record_bytes,
    event_filename,
    record_from_bytes,
)

__all__ = ["EVENT_MODE", "HEAD_MODE", "GenesisValidator", "SuccessorValidator", "ValidatedChain", "ChainStore"]

#: Event files are published world-readable-but-not-writable; HEAD is a plain pointer.
EVENT_MODE: Final[int] = 0o644
HEAD_MODE: Final[int] = 0o644

#: Domain rules are injected, so the chain stays a mechanism (PLAN.md §8).
GenesisValidator = Callable[[ChainRecord], None]
SuccessorValidator = Callable[[ChainRecord, ChainRecord], None]


@dataclass(frozen=True, slots=True)
class ValidatedChain:
    """A fully walked chain: genesis first, head last."""

    records: tuple[ChainRecord, ...]
    revisions: tuple[str, ...]
    genesis_revision: str
    head_revision: str
    identity: str


def _default_validate_genesis(record: ChainRecord) -> None:
    """Mechanical genesis rule: the tail must legitimately be a first record."""
    if record.previous_revision != "":
        raise InvalidSuccessorError("the genesis record must not name a predecessor")


def _default_validate_successor(previous: ChainRecord, record: ChainRecord) -> None:
    """No-op: mechanical continuity is checked by the walk; domain rules are injected."""
    return None


class ChainStore:
    """An append-only, content-addressed chain rooted at ``directory``."""

    def __init__(
        self,
        directory: Path | str,
        *,
        lineage_id: str = "",
        read_only: bool = False,
        maintenance_lock_path: Path | str | None = None,
        validate_genesis: GenesisValidator | None = None,
        validate_successor: SuccessorValidator | None = None,
    ) -> None:
        self._directory = Path(directory)
        self._lineage_id = lineage_id
        self._read_only = read_only
        self._maintenance_lock_path = (
            None if maintenance_lock_path is None else Path(maintenance_lock_path)
        )
        self._validate_genesis = validate_genesis or _default_validate_genesis
        self._validate_successor = validate_successor or _default_validate_successor

    # ── layout ───────────────────────────────────────────────────────────────

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def events_dir(self) -> Path:
        return self._directory / EVENTS_DIRNAME

    @property
    def head_path(self) -> Path:
        return self._directory / HEAD_FILENAME

    @property
    def lock_path(self) -> Path:
        return self._directory / LOCK_FILENAME

    def event_path(self, revision: str) -> Path:
        return self.events_dir / event_filename(revision)

    # ── reading ──────────────────────────────────────────────────────────────

    def read_head(self) -> str:
        """Return the head revision, or ``""`` when **no HEAD file exists**.

        Only an absent HEAD file is a legal empty start.  HEAD is a single line:
        anything else is corruption, never a silent reset to empty (invariant
        I10), and that includes a HEAD file that is *present* but holds nothing
        — an empty, newline-only or whitespace-only file.  Reading that as ``""``
        would answer "proved empty" to "unknown pointer state" and let the next
        genesis append move HEAD off a live lineage while that lineage's events
        stayed on disk: a silent fork.  It raises instead (fail closed).
        """
        data = read_file_no_follow(self.head_path)
        if data is None:
            return ""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChainIntegrityError(f"chain HEAD at {str(self.head_path)!r} is not UTF-8") from exc
        revision = text.strip()
        if not revision:
            raise ChainIntegrityError(
                f"chain HEAD at {str(self.head_path)!r} exists but carries no revision "
                f"({data!r}); an unknown pointer state is not an absent chain"
            )
        if not is_revision(revision):
            raise ChainIntegrityError(
                f"invalid chain HEAD {revision!r} at {str(self.head_path)!r}"
            )
        return revision

    def chain_identity(self) -> str:
        """Order-sensitive identity of the whole stored chain."""
        head = self.read_head()
        if head == "":
            raise ChainError("cannot compute a chain identity: no chain is present")
        return self.load_chain(head).identity

    def load_chain(self, head_revision: str) -> ValidatedChain:
        """Walk from ``head_revision`` to its genesis, validating every link.

        Hostile input is refused rather than truncated: a predecessor cycle, an
        invalid predecessor revision, a discontinuity, and a genesis that the
        injected genesis validator refuses all raise.

        The head's **name is bound to its bytes**: the last check of the walk
        compares the requested ``head_revision`` with the revision computed from
        the head file's content, so a head file whose bytes were swapped for a
        different (even well-formed) record fails closed instead of serving the
        forged payload under the untampered name.  Every ancestor is already
        bound that way by the child naming it; the head has no child, so the
        walk must do it here.

        Residue, stated honestly: this binds the head to its *content*, not the
        chain to an external witness.  Rolling ``HEAD`` back to an older, intact
        record — one whose bytes still reproduce its own name — produces a chain
        that is internally consistent and is NOT caught by this check or by any
        other check in this module: HEAD is the anchor of trust here, so
        detecting a rollback needs an anchor outside the store (a signed
        checkpoint, a monotonic counter held elsewhere, or a witness log).
        Nothing in this wave provides that.
        """
        if not is_revision(head_revision):
            raise ChainIntegrityError(f"chain HEAD revision {head_revision!r} is invalid")

        visited: set[str] = set()
        reverse_records: list[ChainRecord] = []
        reverse_revisions: list[str] = []
        revision = head_revision
        while revision:
            if revision in visited:
                raise ChainIntegrityError(f"chain predecessor cycle detected at {revision!r}")
            visited.add(revision)
            record, loaded_revision = self._load_revision(revision)
            reverse_records.append(record)
            reverse_revisions.append(loaded_revision)
            if record.previous_revision and not is_revision(record.previous_revision):
                raise ChainIntegrityError(
                    f"chain record {revision!r} names an invalid predecessor "
                    f"{record.previous_revision!r}"
                )
            revision = record.previous_revision

        records = tuple(reversed(reverse_records))
        revisions = tuple(reversed(reverse_revisions))
        # The head has no child to bind its name to its bytes, so the walk does
        # it.  This must stay OUTSIDE the loop above: a cycle or a discontinuity
        # is refused while walking, and this comparison is about the *first*
        # revision the walk was asked for.  Cost is one string comparison — the
        # digest was already computed for the head by ``_load_revision``.
        if head_revision != revisions[-1]:
            raise ChainIntegrityError(
                f"chain head {head_revision!r} does not match the revision computed "
                f"from the bytes of that event file ({revisions[-1]!r}): the head "
                "record's name is not bound to its content"
            )
        if not records:
            raise ChainIntegrityError("chain must have exactly one valid genesis record")
        self._validate_genesis(records[0])
        for index in range(1, len(records)):
            if records[index].previous_revision != revisions[index - 1]:
                raise ChainIntegrityError(
                    f"chain predecessor revision is discontinuous at {revisions[index]!r}: "
                    f"it names {records[index].previous_revision!r} but "
                    f"{revisions[index - 1]!r} is what precedes it"
                )
            self._validate_successor(records[index - 1], records[index])

        return ValidatedChain(
            records=records,
            revisions=revisions,
            genesis_revision=revisions[0],
            head_revision=head_revision,
            identity=chain_identity(revisions),
        )

    def _load_revision(self, revision: str) -> tuple[ChainRecord, str]:
        """Read one event file and return ``(record, revision_computed_from_bytes)``."""
        path = self.event_path(revision)
        data = read_file_no_follow(path)
        if data is None:
            raise ChainIntegrityError(f"chain event {revision!r} is missing at {str(path)!r}")
        record = record_from_bytes(data)
        return record, record_revision(data)

    # ── writing ──────────────────────────────────────────────────────────────

    def append(self, expected_revision: str, record: ChainRecord) -> str:
        """CAS-append ``record`` on top of ``expected_revision``; return its revision.

        ``expected_revision`` is mandatory and never defaulted (invariant I3): pass
        ``""`` to claim the genesis slot, or the current HEAD to extend the chain.
        Re-appending an already-committed byte-identical record is idempotent and
        returns the same revision without a second event or a second HEAD move (I5);
        the same revision with divergent stored bytes fails closed (I6).
        """
        if self._read_only:
            operation = record.operation.strip() or "review/append"
            raise LegacyReadOnlyError(operation=operation, lineage_id=self._lineage_id)
        if not str(self._directory).strip():
            raise ChainRecordError("chain store directory is required")
        if not record.operation.strip():
            raise ChainRecordError("record operation is required")

        sealed = replace(record, schema=RECORD_SCHEMA, previous_revision=expected_revision)
        payload = canonical_record_bytes(sealed)
        revision = record_revision(payload)

        mkdir_all_sync(self.events_dir)

        maintenance: StoreLock | None = None
        lock: StoreLock | None = None
        try:
            if self._maintenance_lock_path is not None:
                # Exclusive hook only: no shared/reader mode is implemented in this wave,
                # so a maintenance holder blocks appends outright (fail closed).
                maintenance = acquire_store_lock(self._maintenance_lock_path)
            lock = acquire_store_lock(self.lock_path)

            current = self.read_head()

            if current == revision:
                # Exact retry: still validate the whole chain, then compare bytes.
                self.load_chain(current)
                read_bytes_verified(self.event_path(revision), payload)
                return revision

            if current != expected_revision:
                raise StaleHeadError(expected=expected_revision, current=current, candidate=revision)

            if current == "":
                self._validate_genesis(sealed)
            else:
                chain = self.load_chain(current)
                self._validate_successor(chain.records[-1], sealed)

            self._publish(revision, payload)
            # HEAD is about to name this event as durable: without syncing events/
            # first, a crash here can leave a durable HEAD pointing at an entry the
            # directory never recorded.
            fsync_dir(self.events_dir)
            write_atomic(self.head_path, (revision + "\n").encode("utf-8"), HEAD_MODE)
            return revision
        finally:
            # Both acquisitions sit INSIDE this try: a `LockContendedError` (or any
            # other failure) from the second one must not leave the first lock held
            # for the life of the process — that leak makes the failed append block
            # every later maintenance acquisition, its own included, and leaks the
            # descriptor.  Release in reverse acquisition order; either lock may
            # still be None when its acquisition is what failed.  `release` is
            # unconditional and idempotent (invariant L5).
            #
            # The releases are NESTED, not sequential: if releasing the store lock
            # raises, the maintenance lock must still be released in the same
            # `finally`.  Sequential form (release(); release()) lets the first
            # failure skip the second, and the leak does not depend on `release`
            # suppressing OSError today — that suppression is an implementation
            # detail of StoreLock, not a contract this finally may rely on.
            try:
                if lock is not None:
                    lock.release()
            finally:
                if maintenance is not None:
                    maintenance.release()

    def _publish(self, revision: str, payload: bytes) -> None:
        """Publish the event under its own digest, never replacing an existing one."""
        event_path = self.event_path(revision)
        fd, tmp_name = tempfile.mkstemp(dir=os.fspath(self.events_dir), prefix=EVENT_TEMP_PREFIX)
        tmp_path = Path(tmp_name)
        try:
            try:
                os.fchmod(fd, EVENT_MODE)
                write_all(fd, payload)
                try:
                    os.fsync(fd)
                except OSError as exc:
                    raise ChainSyncError(tmp_path, "event file sync", exc) from exc
            finally:
                os.close(fd)
            try:
                write_immutable_no_replace(tmp_path, event_path)
            except FileExistsError:
                # The name is the digest of the bytes, so an existing file must hold
                # exactly these bytes; anything else is corruption (invariant I6).
                read_bytes_verified(event_path, payload)
        finally:
            with suppress(OSError):
                tmp_path.unlink()
