"""Domain-separated, length-prefixed hashing (wave G1b, mechanism M1c).

Every variable-length field that enters a digest is framed by
:func:`write_length_prefixed` — ``<decimal byte length> NUL <value> NUL`` — and every
digest is prefixed by a domain tag followed by exactly one ``NUL``. That makes
concatenation ambiguity impossible (``["ab","c"]`` cannot collide with ``["a","bc"]``)
and keeps the identities of two different purposes from ever colliding, even when the
field bytes coincide.

The framing is a byte-level port of the Go reference
(``reviewtransaction/snapshot.go:1661-1677`` for ``hashCanonical`` /
``writeLengthPrefixed``, ``store.go:823-831`` for ``chainIdentity``), re-domained for
Antigona: the domain tags below are Antigona's own and are versioned — a tag is never
reused for a second purpose and never edited in place.

Digest shape is owned by :func:`antigona.skills.canonical.is_sha256_hex`; this module
re-exports nothing and adds no second predicate (invariant H6).
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from typing import Final, Protocol

from antigona.skills.canonical import is_sha256_hex

__all__ = [
    "DOMAIN_CHAIN_IDENTITY",
    "DOMAIN_CHAIN_RECORD",
    "DOMAIN_FIELD",
    "DOMAIN_SEPARATOR",
    "FIELD_SEPARATOR",
    "REVISION_PREFIX",
    "chain_identity",
    "domain_digest",
    "is_revision",
    "record_revision",
    "write_length_prefixed",
]

#: The only prefix a stored revision may carry, mirroring the reference format.
REVISION_PREFIX: Final[str] = "sha256:"

#: Domain tags. Byte-exact, versioned, never reused across purposes.
DOMAIN_CHAIN_IDENTITY: Final[bytes] = b"antigona.review-chain/v1"
DOMAIN_CHAIN_RECORD: Final[bytes] = b"antigona.review-record/v1"
DOMAIN_FIELD: Final[bytes] = b"antigona.field/v1"

#: The single framing byte: exactly one NUL after the domain tag, exactly one NUL
#: after the decimal length and after each value.
DOMAIN_SEPARATOR: Final[bytes] = b"\x00"
FIELD_SEPARATOR: Final[bytes] = b"\x00"


class _ByteWriter(Protocol):
    """Any incremental hash object (``hashlib`` and friends).

    ``update`` is declared positional-only so that ``hashlib``'s own
    ``update(obj: ReadableBuffer, /)`` conforms without a name clash.
    """

    def update(self, data: bytes, /) -> None: ...


def write_length_prefixed(writer: _ByteWriter, value: bytes) -> None:
    """Frame one field unambiguously: ``<decimal length> NUL <value> NUL``.

    This is the *only* way a variable-length value may enter a digest (invariant
    H1); a bare concatenation anywhere in this package is a bug.
    """
    writer.update(str(len(value)).encode("ascii"))
    writer.update(FIELD_SEPARATOR)
    writer.update(value)
    writer.update(FIELD_SEPARATOR)


def domain_digest(domain: bytes, *fields: bytes) -> str:
    """``sha256`` over ``domain + NUL`` then each field length-prefixed.

    Returns ``"sha256:<hex>"``. An empty field list is legal and yields the digest
    of the bare domain tag.
    """
    digest = sha256()
    digest.update(domain + DOMAIN_SEPARATOR)
    for field in fields:
        write_length_prefixed(digest, field)
    return REVISION_PREFIX + digest.hexdigest()


def chain_identity(revisions: Sequence[str]) -> str:
    """Order-sensitive identity of an ordered revision list.

    The identity is *not* the head revision: it binds the whole lineage, so two
    chains that happen to share a head cannot share an identity.
    """
    return domain_digest(
        DOMAIN_CHAIN_IDENTITY, *[revision.encode("utf-8") for revision in revisions]
    )


def record_revision(payload: bytes) -> str:
    """``"sha256:" + sha256(payload).hexdigest()`` — the content address itself.

    ``payload`` must be the exact byte range that is stored (see
    :func:`antigona.chain.records.canonical_record_bytes`), so that a file's name is
    the digest of its own content.
    """
    return REVISION_PREFIX + sha256(payload).hexdigest()


def is_revision(value: str) -> bool:
    """True iff ``value`` is ``"sha256:" + 64 lowercase hex``.

    Digest-shape authority stays in :func:`antigona.skills.canonical.is_sha256_hex`.
    """
    if not value.startswith(REVISION_PREFIX):
        return False
    return is_sha256_hex(value[len(REVISION_PREFIX) :])
