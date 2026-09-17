"""The chain record and its canonical byte form (wave G1b).

The record is *sealed* before it is hashed: ``previous_revision`` is part of the
hashed range, so re-writing the predecessor link changes the digest (invariant R3).
The file is then named after its own digest (R1), which is what makes the chain
content-addressed rather than merely append-only.

Deliberate non-goal: the *domain* meaning of an operation is not decided here. Any
successor rules (which operation may follow which) are injected into
:class:`antigona.chain.store.ChainStore` as callables, so this package stays a
mechanism.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .errors import ChainIntegrityError, ChainRecordError

__all__ = [
    "EVENTS_DIRNAME",
    "EVENT_SUFFIX",
    "HEAD_FILENAME",
    "LOCK_FILENAME",
    "RECORD_SCHEMA",
    "ChainRecord",
    "canonical_record_bytes",
    "event_filename",
    "record_from_bytes",
]

#: The record envelope schema. A stored record with any other schema is corruption.
RECORD_SCHEMA: Final[str] = "antigona.review-record/v1"

HEAD_FILENAME: Final[str] = "HEAD"
EVENTS_DIRNAME: Final[str] = "events"
LOCK_FILENAME: Final[str] = "LOCK"
EVENT_SUFFIX: Final[str] = ".json"

_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {"schema", "operation", "previous_revision", "payload"}
)


@dataclass(frozen=True, slots=True)
class ChainRecord:
    """One immutable link of the chain.

    ``previous_revision`` is ``""`` only for the genesis record; every other record
    names the exact revision it extends. ``payload`` is the domain body — validated
    by the caller's successor rules, never by this module.
    """

    operation: str
    previous_revision: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)
    schema: str = RECORD_SCHEMA

    def __post_init__(self) -> None:
        if not self.operation or not self.operation.strip():
            raise ChainRecordError("record operation is required")
        if not isinstance(self.previous_revision, str):
            raise ChainRecordError("record previous_revision must be a string")
        if not isinstance(self.payload, Mapping):
            raise ChainRecordError("record payload must be a mapping")


def canonical_record_bytes(record: ChainRecord) -> bytes:
    """The exact hashed/stored byte range: deterministic JSON + one trailing LF.

    ``sort_keys=True`` makes the byte form independent of mapping insertion order,
    so two equal records always produce one file. The trailing newline is part of
    the address: it is inside the digest, so a store written without it is a
    different store.
    """
    document: dict[str, object] = {
        "schema": record.schema,
        "operation": record.operation,
        "previous_revision": record.previous_revision,
        "payload": dict(record.payload),
    }
    try:
        text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ChainRecordError(f"record payload is not canonically serialisable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def event_filename(revision: str) -> str:
    """``"<64-hex>.json"`` — the event file is named after its *own* digest.

    The events directory is flat: the name is already a full-strength digest, so
    sharding would only add a directory level between a chain walk and its file.
    """
    return revision[len("sha256:") :] + EVENT_SUFFIX


def record_from_bytes(data: bytes) -> ChainRecord:
    """Parse stored event bytes back into a :class:`ChainRecord`.

    Any structural divergence raises :class:`ChainIntegrityError` (never a silent
    default): a chain whose bytes are not a record is a corrupt chain.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChainIntegrityError(f"stored chain event is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChainIntegrityError(f"stored chain event is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ChainIntegrityError("stored chain event must be a JSON object")

    missing = _RECORD_KEYS - set(document)
    if missing:
        raise ChainIntegrityError(f"stored chain event is missing keys: {sorted(missing)}")
    schema = document["schema"]
    if schema != RECORD_SCHEMA:
        raise ChainIntegrityError(
            f"stored chain event schema {schema!r} is not {RECORD_SCHEMA!r}"
        )
    operation = document["operation"]
    if not isinstance(operation, str) or not operation.strip():
        raise ChainIntegrityError("stored chain event has no operation")
    previous_revision = document["previous_revision"]
    if not isinstance(previous_revision, str):
        raise ChainIntegrityError("stored chain event previous_revision is not a string")
    payload = document["payload"]
    if not isinstance(payload, dict):
        raise ChainIntegrityError("stored chain event payload is not a JSON object")
    return ChainRecord(
        schema=schema,
        operation=operation,
        previous_revision=previous_revision,
        payload=payload,
    )
