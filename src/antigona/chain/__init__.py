"""Content-addressed, append-only chain with a HEAD pointer and CAS.

Public surface (wave G1b):

* :mod:`antigona.chain.hashing` — domain-separated length-prefixed hashing (M1c);
* :mod:`antigona.chain.records` — the sealed record and its canonical bytes;
* :mod:`antigona.chain.fsops` — durability / no-replace primitives (M1a);
* :mod:`antigona.chain.locking` — the bounded advisory lock and its typed refusals;
* :mod:`antigona.chain.store` — :class:`~antigona.chain.store.ChainStore`;
* :mod:`antigona.chain.errors` — the typed error taxonomy (M2).

The two refusal identities callers must tell apart: a lost compare-and-swap
(:class:`~antigona.chain.errors.StaleHeadError`, a
:class:`~antigona.durable.state_machine.ConcurrentUpdate` — mutation **unknown**) versus
proven-not-started lock contention
(:class:`~antigona.chain.errors.LockContendedError` — the guarded body never ran).
"""

from __future__ import annotations

from .errors import (
    ChainError,
    ChainIntegrityError,
    ChainPathError,
    ChainRecordError,
    ChainSyncError,
    InvalidSuccessorError,
    LegacyReadOnlyError,
    LockContendedError,
    LockPreAcquisitionError,
    StaleHeadError,
)
from .hashing import (
    DOMAIN_CHAIN_IDENTITY,
    DOMAIN_CHAIN_RECORD,
    DOMAIN_FIELD,
    chain_identity,
    domain_digest,
    is_revision,
    record_revision,
    write_length_prefixed,
)
from .locking import LockOwner, StoreLock, acquire_store_lock, is_lock_contention
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
from .store import ChainStore, ValidatedChain

__all__ = [
    "DOMAIN_CHAIN_IDENTITY",
    "DOMAIN_CHAIN_RECORD",
    "DOMAIN_FIELD",
    "EVENTS_DIRNAME",
    "HEAD_FILENAME",
    "LOCK_FILENAME",
    "RECORD_SCHEMA",
    "ChainError",
    "ChainIntegrityError",
    "ChainPathError",
    "ChainRecord",
    "ChainRecordError",
    "ChainStore",
    "ChainSyncError",
    "InvalidSuccessorError",
    "LegacyReadOnlyError",
    "LockContendedError",
    "LockOwner",
    "LockPreAcquisitionError",
    "StaleHeadError",
    "StoreLock",
    "ValidatedChain",
    "acquire_store_lock",
    "canonical_record_bytes",
    "chain_identity",
    "domain_digest",
    "event_filename",
    "is_lock_contention",
    "is_revision",
    "record_from_bytes",
    "record_revision",
    "write_length_prefixed",
]
