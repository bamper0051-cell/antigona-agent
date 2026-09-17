"""In-memory representation of an ``ASKILL/1`` card.

One frozen record per grammar production of docs/SKILL_FORMAT.md §3, so a parsed card
is immutable data — never code, never a template to execute. Ordering is preserved with
tuples because the canonical byte form (§4) applies no sorting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class HeredocText:
    """A ``<<`` … ``>>`` plan argument: ordered lines with the 4-space indent stripped.

    Kept distinct from a plain ``qstring`` so the canonical form is a function of the card
    alone: a heredoc always renders back as a heredoc, never as a quoted one-liner.
    """

    lines: tuple[str, ...]


#: Value of a slot default or a plan argument: ``qstring``, ``int-literal``, ``bool`` or heredoc.
CardValue = str | int | bool | HeredocText


class Trust(StrEnum):
    """``%trust`` — trust label inherited from the capture trajectory (P1.3 degradation)."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


#: Alias for Trust
SkillTrust = Trust


class RiskCeiling(StrEnum):
    """``%risk`` — highest risk level a step of this card may reach."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


#: Alias for RiskCeiling
SkillRisk = RiskCeiling



class MatchMode(StrEnum):
    """``[match] mode=`` — whether all rules or any single rule must fire."""

    ALL = "all"
    ANY = "any"


class MatchKind(StrEnum):
    """Structural trigger rules; the matcher never reads ``[intent]`` prose."""

    KEYWORD = "kw"
    PATH_PREFIX = "path-prefix"
    TOOL_AVAILABLE = "tool-available"


class SlotType(StrEnum):
    """``[slots]`` value types."""

    PATH = "path"
    TEXT = "text"
    INT = "int"
    BOOL = "bool"


class ClaimKind(StrEnum):
    """``[claims]`` — assertions about the card's own effect, verified before promotion.

    These are not task acceptance criteria: those live only in ``VerifierCriteriaStore``.
    """

    PRODUCES_FILE = "produces-file"
    PRODUCES_NONEMPTY = "produces-nonempty"
    NO_NETWORK = "no-network"


class Verdict(StrEnum):
    """``[origin] verdict`` — v1 admits a single value."""

    VERIFIER_PASS = "verifier-pass"


@dataclass(frozen=True, slots=True)
class MatchRule:
    """One ``[match]`` rule; ``values`` holds the alternatives of a ``kw`` rule, else one item."""

    kind: MatchKind
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Require:
    """``[require]`` — execution limits and required workspace state, as ``(key, value)`` pairs."""

    max_steps: int | None = None
    max_bytes: int | None = None
    states: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class Slot:
    """One ``[slots]`` declaration; the only substitution form is ``{{slot:NAME}}``."""

    name: str
    type: SlotType
    required: bool
    default: CardValue | None = None


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One ``[plan]`` step: a registered tool name plus ordered, uniquely keyed arguments.

    The tool name is checked against the Worker tool registry at promotion time, not here.
    """

    number: int
    tool: str
    args: tuple[tuple[str, CardValue], ...] = ()


@dataclass(frozen=True, slots=True)
class Claim:
    """One ``[claims]`` assertion; ``value`` is a workspace path or a boolean per kind."""

    kind: ClaimKind
    value: CardValue


@dataclass(frozen=True, slots=True)
class Origin:
    """``[origin]`` — provenance of the capture that produced this card."""

    flow: str
    steps: int
    captured: datetime
    verdict: Verdict
    trust_at_capture: Trust


@dataclass(frozen=True, slots=True)
class SkillCard:
    """A whole parsed card. Optional sections are absent as ``None`` or an empty tuple."""

    skill_id: str
    slug: str
    version: int
    owner_id: str
    trust: Trust
    risk: RiskCeiling
    intent: tuple[str, ...]
    plan: tuple[PlanStep, ...]
    origin: Origin
    format_version: int = 1
    match_mode: MatchMode | None = None
    match: tuple[MatchRule, ...] = ()
    require: Require | None = None
    slots: tuple[Slot, ...] = ()
    claims: tuple[Claim, ...] = ()
