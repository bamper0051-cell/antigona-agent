"""Skill registry and ``ASKILL/1`` format errors.

Every parse/validation failure carries the normative error code from
docs/SKILL_FORMAT.md §8; the code is a class attribute so a caller can branch on the
exception type and still journal the documented string. There are no warnings in the
format: each code is a hard, fail-closed refusal.
"""

from __future__ import annotations

from typing import ClassVar


class SkillNotFound(Exception):
    """Raised when a skill id has no row in the registry."""


class SkillFormatError(Exception):
    """Base class for ``ASKILL/1`` parse and validation failures."""

    code: ClassVar[str] = "E-SYNTAX"

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.message = message
        self.line = line
        location = "" if line is None else f" line {line}"
        super().__init__(f"{self.code}{location}: {message}")


class SkillEncodingError(SkillFormatError):
    """``E-ENC`` — BOM, CR/CRLF, TAB, control characters or non-UTF-8 bytes."""

    code: ClassVar[str] = "E-ENC"


class SkillLimitError(SkillFormatError):
    """``E-LIMIT`` — line, file, step, slot or ``require.max-steps`` limit exceeded."""

    code: ClassVar[str] = "E-LIMIT"


class SkillOrderError(SkillFormatError):
    """``E-ORDER`` — duplicated section, misordered sections or a gap in step numbering."""

    code: ClassVar[str] = "E-ORDER"


class SkillUnknownError(SkillFormatError):
    """``E-UNKNOWN`` — unknown ``%``-directive, section, key or enumeration value."""

    code: ClassVar[str] = "E-UNKNOWN"


class SkillSlotError(SkillFormatError):
    """``E-SLOT`` — undeclared slot reference, default type mismatch, escaping or unused slot."""

    code: ClassVar[str] = "E-SLOT"


class SkillHashError(SkillFormatError):
    """``E-HASH`` — ``%end`` digest or byte count does not match the hashed range."""

    code: ClassVar[str] = "E-HASH"


class SkillSyntaxError(SkillFormatError):
    """``E-SYNTAX`` — grammar violation, non-canonical stored bytes or a §7.2 prohibition."""

    code: ClassVar[str] = "E-SYNTAX"


class SkillVersionError(SkillFormatError):
    """``E-VERSION`` — missing/damaged ``%ASKILL`` line or a major version this parser refuses."""

    code: ClassVar[str] = "E-VERSION"


class SkillCriteriaError(SkillFormatError):
    """``E-CRITERIA`` — reserved verification-criteria section, directive or key (ADR-0004 boundary)."""

    code: ClassVar[str] = "E-CRITERIA"


class SkillIntegrityError(SkillHashError):
    """Stored card body diverges from its recorded hash; the skill must be quarantined."""
