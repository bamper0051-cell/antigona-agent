"""Canonical byte form of a card and its integrity hash.

Implemented in P2.1.e per docs/SKILL_FORMAT.md §4 (column alignment, exactly one blank
line between blocks) and §6 (the ``%end sha256:… bytes=…`` range runs from the first
byte of the file up to and including the ``LF`` preceding the footer line).

This module is the byte/lexeme layer: it never looks at the grammar, so ``format.py``
may import it freely. The single back edge is :func:`canonicalize`, which needs the
parser and therefore imports it lazily inside the function body.
"""

from __future__ import annotations

import hashlib

from .errors import SkillHashError, SkillSyntaxError

HEX_DIGITS = frozenset("0123456789abcdef")

_FOOTER_HEAD = "%end sha256:"
_FOOTER_MID = " bytes="
#: Length of a sha256 hex digest (footer and content-addressed body keys).
DIGEST_LEN = 64
# Back-compat private alias — prefer :data:`DIGEST_LEN`.
_DIGEST_LEN = DIGEST_LEN


def is_uint(text: str) -> bool:
    """``uint`` per §3: at least one digit, no leading zero unless the value is ``0``."""
    if not text or not text.isascii() or not text.isdigit():
        return False
    return text == "0" or text[0] != "0"


def is_sha256_hex(text: str) -> bool:
    """Return ``True`` iff ``text`` is a 64-character lowercase hex digest.

    Single home for digest-shape checks used by the footer parser and the body store.
    """
    return len(text) == DIGEST_LEN and all(char in HEX_DIGITS for char in text)


def split_footer(data: bytes) -> tuple[bytes, str, int]:
    """Split raw file bytes into the hashed range **H**, the footer text and its line number.

    **H** ends with the ``LF`` that precedes the ``%end`` line, so ``len(H)`` is exactly the
    offset of the first byte of that line (§6.1).
    """
    if not data.endswith(b"\n"):
        raise SkillSyntaxError("file must end with a single LF")
    cut = data.rfind(b"\n", 0, len(data) - 1)
    if cut < 0:
        raise SkillSyntaxError("file has no %end footer line", line=1)
    head = data[: cut + 1]
    try:
        footer = data[cut + 1 : -1].decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - encoding gate runs first
        raise SkillSyntaxError("footer line is not valid UTF-8") from exc
    return head, footer, head.count(b"\n") + 1


def parse_footer(footer: str, line: int) -> tuple[str, int]:
    """Validate ``%end sha256:<64 hex> bytes=<uint>`` and return the declared pair."""
    if not footer.startswith(_FOOTER_HEAD):
        raise SkillSyntaxError("last line must be a '%end sha256:… bytes=…' footer", line=line)
    rest = footer[len(_FOOTER_HEAD) :]
    digest = rest[:DIGEST_LEN]
    if not is_sha256_hex(digest):
        raise SkillSyntaxError("footer digest must be 64 lowercase hex characters", line=line)
    tail = rest[DIGEST_LEN:]
    if not tail.startswith(_FOOTER_MID):
        raise SkillSyntaxError("footer digest must be followed by ' bytes='", line=line)
    count = tail[len(_FOOTER_MID) :]
    if not is_uint(count):
        raise SkillSyntaxError("footer byte count must be a decimal integer", line=line)
    return digest, int(count)


def body_digest(data: bytes) -> tuple[str, int]:
    """Return ``(sha256_hex, byte_count)`` of the hashed range **H** of a whole card file."""
    head, _, _ = split_footer(data)
    return hashlib.sha256(head).hexdigest(), len(head)


def footer_line(head: bytes) -> bytes:
    """Build the ``%end`` line (with its trailing ``LF``) for the hashed range ``head``."""
    digest = hashlib.sha256(head).hexdigest()
    return f"{_FOOTER_HEAD}{digest}{_FOOTER_MID}{len(head)}\n".encode()


def verify_footer(data: bytes) -> tuple[str, int]:
    """Check the ``%end`` footer against the actual bytes; raise ``E-HASH`` on divergence.

    Integrity is checked on the raw bytes *before* parsing (§6.3) so that a tampered card
    can never be "healed" by canonicalisation.
    """
    head, footer, line = split_footer(data)
    declared_digest, declared_bytes = parse_footer(footer, line)
    actual_digest = hashlib.sha256(head).hexdigest()
    actual_bytes = len(head)
    if declared_bytes != actual_bytes:
        raise SkillHashError(
            f"footer declares bytes={declared_bytes} but the hashed range is {actual_bytes}",
            line=line,
        )
    if declared_digest != actual_digest:
        raise SkillHashError("footer sha256 does not match the card body", line=line)
    return actual_digest, actual_bytes


def canonicalize(data: bytes) -> bytes:
    """Return the canonical byte form of a valid card (§4).

    Equivalent to ``render_card(parse_card(data))`` and idempotent: canonicalising an
    already canonical card returns it unchanged.
    """
    from .format import parse_card, render_card

    return render_card(parse_card(data))
