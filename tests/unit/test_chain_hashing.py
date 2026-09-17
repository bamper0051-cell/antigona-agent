"""Unit tests for the domain-separated length-prefixed hashing layer (wave G1b, M1c).

Invariants under test (PLAN.md §4.1): H1 length prefix is mandatory, H2 concatenation
ambiguity is impossible, H3 domain separation, H4 order sensitivity, H5 domain tag
followed by exactly one NUL, H6 digest shape (reusing ``skills.canonical.is_sha256_hex``).
"""

from __future__ import annotations

from hashlib import sha256

from antigona.chain import hashing
from antigona.skills.canonical import is_sha256_hex


class _Recorder:
    """Minimal ``byteWriter``: records the exact byte stream written."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def update(self, data: bytes) -> None:
        self.buffer += data


def _framing(domain: bytes, values: list[bytes]) -> bytes:
    """Independent transcription of the framing rule (Go snapshot.go:1661-1677)."""
    out = bytearray(domain + b"\x00")
    for value in values:
        out += str(len(value)).encode()
        out += b"\x00"
        out += value
        out += b"\x00"
    return bytes(out)


# ── 1. H1 — the length prefix is mandatory and byte-exact ────────────────────


def test_write_length_prefixed_frames_value_with_length_and_nul() -> None:
    writer = _Recorder()
    hashing.write_length_prefixed(writer, b"abc")
    assert bytes(writer.buffer) == b"3\x00abc\x00"

    empty = _Recorder()
    hashing.write_length_prefixed(empty, b"")
    assert bytes(empty.buffer) == b"0\x00\x00"

    multibyte = _Recorder()
    hashing.write_length_prefixed(multibyte, "пять".encode())
    assert bytes(multibyte.buffer) == b"8\x00" + "пять".encode() + b"\x00"


# ── 2. H2 — no concatenation ambiguity ───────────────────────────────────────


def test_chain_identity_disambiguates_field_boundaries() -> None:
    left = hashing.chain_identity(["ab", "c"])
    right = hashing.chain_identity(["a", "bc"])
    assert left != right


# ── 3. H4 — order sensitivity ────────────────────────────────────────────────


def test_chain_identity_is_order_sensitive() -> None:
    assert hashing.chain_identity(["r1", "r2"]) != hashing.chain_identity(["r2", "r1"])


# ── 4. H3 / H5 — domain separation and explicit framing ──────────────────────


def test_chain_identity_domain_tag_separates_from_record_digest() -> None:
    payload = b"same-field"
    identity = hashing.chain_identity(["same-field"])
    record = hashing.record_revision(payload)
    assert identity != record
    assert hashing.domain_digest(hashing.DOMAIN_CHAIN_IDENTITY, payload) == identity
    assert (
        hashing.domain_digest(hashing.DOMAIN_CHAIN_RECORD, payload)
        != hashing.domain_digest(hashing.DOMAIN_CHAIN_IDENTITY, payload)
    )
    # Exact separation, not a disjunction: the identity of a field list is the
    # digest of the DOMAIN-TAGGED, length-prefixed framing, while the record
    # revision is the bare digest of the payload with no domain at all.  The
    # previous form of this check was ``A == B or C != D``, where the first
    # disjunct was false, so only the second ever held and the assertion could
    # not fail for the reason it claimed.  Each side is now pinned against its
    # own independent construction, so dropping the domain tag from either
    # purpose breaks this test.
    assert identity == "sha256:" + sha256(
        _framing(hashing.DOMAIN_CHAIN_IDENTITY, [payload])
    ).hexdigest()
    assert record == "sha256:" + sha256(payload).hexdigest()
    # (`identity != record` was asserted twice — here and above with the two digests
    # freshly named.  The second copy could never fail on its own, so it is gone.)
    # A longer field list is framed (and therefore tagged) differently, so it can
    # never collide with the single-field identity it is compared against.
    assert identity != hashing.chain_identity(["same-field", ""])


# ── 5. Frozen golden vector ──────────────────────────────────────────────────


def test_chain_identity_golden_vector() -> None:
    """Frozen hex for ["sha256:aa", "sha256:bb"] — a framing change is a hard failure.

    Reference computed independently (raw evidence:
    ``waveG1b_chain_impl_*/golden_reference.log``), not by calling this module.
    """
    assert (
        hashing.chain_identity(["sha256:aa", "sha256:bb"])
        == "sha256:43fb88ba2d2a5f00d82aef19ab01c57b338497761d3f62dec1ba7e65024c37a5"
    )
    assert (
        hashing.chain_identity([])
        == "sha256:a94092f5637a0ba2fbd66a7e68ab285dd0e15bd93d0871b5e95dc87ed41c7cab"
    )
    # The framing itself, byte for byte (domain + exactly one NUL before the first prefix).
    assert _framing(
        b"antigona.review-chain/v1", [b"sha256:aa", b"sha256:bb"]
    ) == b"antigona.review-chain/v1\x009\x00sha256:aa\x009\x00sha256:bb\x00"


# ── 6. H6 — digest shape and determinism ─────────────────────────────────────


def test_domain_digest_is_deterministic_and_returns_sha256_prefix() -> None:
    first = hashing.domain_digest(hashing.DOMAIN_FIELD, b"a", b"bc")
    second = hashing.domain_digest(hashing.DOMAIN_FIELD, b"a", b"bc")
    assert first == second
    assert first.startswith("sha256:")
    assert is_sha256_hex(first[len("sha256:") :])
    assert not is_sha256_hex(first[len("sha256:") :].upper())

    assert hashing.domain_digest(hashing.DOMAIN_FIELD, b"ab", b"c") != hashing.domain_digest(
        hashing.DOMAIN_FIELD, b"a", b"bc"
    )

    revision = hashing.record_revision(b"{}\n")
    assert revision.startswith("sha256:")
    assert is_sha256_hex(revision[len("sha256:") :])
    assert hashing.is_revision(revision)
    assert not hashing.is_revision(revision.upper())
    assert not hashing.is_revision("")
    assert not hashing.is_revision(revision[len("sha256:") :])
