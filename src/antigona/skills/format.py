"""``ASKILL/1`` parser and serializer — ``parse_card(bytes) -> SkillCard`` and
``render_card(SkillCard) -> bytes``.

Implemented in P2.1.e per docs/SKILL_FORMAT.md §§2-5, 7-8: single-pass line-based
parsing, fail-closed on the first error, every refusal carrying a code and a 1-based
line number.

The parser is deliberately permissive only where §4 says it must be — at ``pad``
positions, where any run of spaces is accepted. Everything else is matched literally,
so a card that is not byte-canonical still parses but never round-trips to itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .canonical import footer_line, is_uint, verify_footer
from .errors import (
    SkillCriteriaError,
    SkillEncodingError,
    SkillLimitError,
    SkillOrderError,
    SkillSlotError,
    SkillSyntaxError,
    SkillUnknownError,
    SkillVersionError,
)
from .records import (
    CardValue,
    Claim,
    ClaimKind,
    HeredocText,
    MatchKind,
    MatchMode,
    MatchRule,
    Origin,
    PlanStep,
    Require,
    RiskCeiling,
    SkillCard,
    Slot,
    SlotType,
    Trust,
    Verdict,
)

__all__ = ["parse_card", "render_card"]

# --- limits (§5) -----------------------------------------------------------------

MAX_FILE_BYTES = 65536
MAX_LINE_BYTES = 200
MAX_PLAN_STEPS = 32
MAX_SLOTS = 16

FORMAT_MAJOR = 1

# --- lexical sets (§3) -----------------------------------------------------------

_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_DIGITS = frozenset("0123456789")
_ALNUM = _LOWER | _DIGITS
_HEX = frozenset("0123456789abcdef")
_OWNER_CHARS = _ALNUM | {"_", "-"}
_IDENT_TAIL = _LOWER | _DIGITS | {"_"}

# --- structure (§3.1, §7.1) ------------------------------------------------------

SECTION_ORDER: tuple[str, ...] = (
    "intent",
    "match",
    "require",
    "slots",
    "plan",
    "claims",
    "origin",
)
REQUIRED_SECTIONS = frozenset({"intent", "plan", "origin"})

_HEADER_DIRECTIVES: tuple[str, ...] = ("%id", "%slug", "%version", "%owner", "%trust", "%risk")
_KNOWN_DIRECTIVES = frozenset(_HEADER_DIRECTIVES) | {"%ASKILL", "%end"}

RESERVED_SECTIONS = frozenset(
    {
        "criteria",
        "criterion",
        "verify",
        "verification",
        "acceptance",
        "checks",
        "expect",
        "expected",
        "definition-of-done",
        "dod",
    }
)
RESERVED_DIRECTIVES = frozenset(
    {"%criteria", "%criterion", "%verify", "%acceptance", "%dod", "%expect"}
)
RESERVED_KEYS = frozenset(
    {
        "criteria",
        "criterion",
        "acceptance_criteria",
        "acceptance-criteria",
        "success_criteria",
        "success-criteria",
        "verification",
        "verification_criteria",
        "expected_verdict",
        "expected-verdict",
        "definition_of_done",
    }
)

_ORIGIN_KEYS: tuple[str, ...] = ("flow", "steps", "captured", "verdict", "trust-at-capture")
_LIMIT_NAMES = frozenset({"max-steps", "max-bytes"})

#: Substitution and interpolation forms that must never survive into a card (§7.2).
_FORBIDDEN_MARKERS: tuple[str, ...] = ("{{", "}}", "${", "$(")

_SLOT_PREFIX = "slot:"
_ARG_PREFIX = "  ~ "
_HEREDOC_OPEN = "<<"
_HEREDOC_CLOSE = "  >>"
_HEREDOC_INDENT = "    "
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# --- canonical field widths (§4) -------------------------------------------------

_W_HEADER = 10
_W_MATCH = 15
_W_REQUIRE = 7
_W_SLOT_NAME = 13
_W_SLOT_TYPE = 7
_W_SLOT_PRESENCE = 10
_W_CLAIM = 19
_W_ORIGIN = 18


def _field(name: str, width: int) -> str:
    """Pad ``name`` to the canonical column, always leaving at least one separator space."""
    return name + " " * max(width - len(name), 1)


# --- token-level predicates ------------------------------------------------------


def _is_ident(text: str) -> bool:
    return bool(text) and text[0] in _LOWER and all(char in _IDENT_TAIL for char in text[1:])


def _is_tool_name(text: str) -> bool:
    parts = text.split(".")
    return len(parts) >= 2 and all(_is_ident(part) for part in parts)


def _is_state_key(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 2 and all(_is_ident(part) for part in parts)


def _is_slug(text: str) -> bool:
    if not text or len(text.encode()) > 64 or text[0] not in _ALNUM:
        return False
    return all(char in _ALNUM or char == "-" for char in text[1:])


def _is_owner_id(text: str) -> bool:
    return bool(text) and len(text.encode()) <= 64 and all(c in _OWNER_CHARS for c in text)


def _is_slot_name(text: str) -> bool:
    if not text or len(text.encode()) > 32 or text[0] not in _LOWER:
        return False
    return all(char in _IDENT_TAIL for char in text[1:])


def _is_uuid(text: str) -> bool:
    groups = text.split("-")
    if [len(group) for group in groups] != [8, 4, 4, 4, 12]:
        return False
    return all(char in _HEX for group in groups for char in group)


def _is_skill_id(text: str) -> bool:
    """Return ``True`` if ``text`` starts with ``skl-`` and has at least one valid identifier char."""
    if not text.startswith("skl-") or len(text) <= 4:
        return False
    return _is_uuid(text[4:]) or all(c in _ALNUM or c == "-" for c in text[4:])


# --- line scanner ----------------------------------------------------------------


class _Scan:
    """Cursor over one line; every failure is an ``E-SYNTAX`` carrying the line number."""

    __slots__ = ("line", "lineno", "pos")

    def __init__(self, line: str, lineno: int) -> None:
        self.line = line
        self.lineno = lineno
        self.pos = 0

    def fail(self, why: str) -> SkillSyntaxError:
        return SkillSyntaxError(why, line=self.lineno)

    def word(self) -> str:
        start = self.pos
        while self.pos < len(self.line) and self.line[self.pos] != " ":
            self.pos += 1
        if self.pos == start:
            raise self.fail("expected a word")
        return self.line[start:self.pos]

    def pad(self) -> None:
        start = self.pos
        while self.pos < len(self.line) and self.line[self.pos] == " ":
            self.pos += 1
        if self.pos == start:
            raise self.fail("expected at least one space")

    def sp(self) -> None:
        if self.pos >= len(self.line) or self.line[self.pos] != " ":
            raise self.fail("expected exactly one space")
        self.pos += 1

    def literal(self, text: str) -> None:
        if not self.line.startswith(text, self.pos):
            raise self.fail(f"expected {text!r}")
        self.pos += len(text)

    def rest(self) -> str:
        value = self.line[self.pos :]
        self.pos = len(self.line)
        if not value:
            raise self.fail("expected a value")
        return value

    def at_end(self) -> bool:
        return self.pos >= len(self.line)

    def done(self) -> None:
        if not self.at_end():
            raise self.fail("unexpected trailing content")


# --- pre-parse gates (§2, §5) ----------------------------------------------------


def _decode(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise SkillLimitError(f"card file is {len(data)} bytes, limit is {MAX_FILE_BYTES}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillEncodingError("card file is not valid UTF-8") from exc


def _scan_encoding(lines: list[str]) -> None:
    for index, line in enumerate(lines, start=1):
        for char in line:
            code = ord(char)
            if char == "\t":
                raise SkillEncodingError("TAB is forbidden anywhere in a card", line=index)
            if char == "\r":
                raise SkillEncodingError("CR is forbidden; line endings are LF only", line=index)
            if code == 0xFEFF:
                raise SkillEncodingError("U+FEFF (BOM) is forbidden", line=index)
            if code < 0x20 or 0x7F <= code <= 0x9F:
                raise SkillEncodingError(f"control character U+{code:04X} is forbidden", line=index)
        if len(line.encode()) > MAX_LINE_BYTES:
            raise SkillLimitError(
                f"line is {len(line.encode())} bytes, limit is {MAX_LINE_BYTES}", line=index
            )
        if line.endswith(" "):
            raise SkillSyntaxError("trailing spaces are forbidden", line=index)


def _reserved_key_guard(key: str, line: int) -> None:
    if key.lower() in RESERVED_KEYS:
        raise SkillCriteriaError(
            f"key {key!r} declares verification criteria, which a card may never carry", line=line
        )


# --- parser ----------------------------------------------------------------------


class _Parser:
    """Single-pass, line-based parser over the body lines (header + sections, no footer)."""

    def __init__(self, body: list[str]) -> None:
        self.body = body
        self.used_slots: list[tuple[str, int]] = []
        self.slot_lines: dict[str, int] = {}
        self.declared: dict[str, Slot] = {}
        self.plan_text: list[tuple[str, int]] = []
        self.trust_line: int | None = None

    # -- helpers ------------------------------------------------------------

    def _line(self, index: int) -> str:
        return self.body[index]

    def _split_pad(self, line: str, prefix: str, lineno: int) -> str:
        scan = _Scan(line, lineno)
        scan.literal(prefix)
        scan.pad()
        return scan.rest()

    def _scan_value(self, text: str, lineno: int) -> None:
        """Record ``{{slot:NAME}}`` uses and reject every other substitution form (§3.2.2)."""
        plain: list[str] = []
        index = 0
        while index < len(text):
            if text.startswith("{{", index):
                close = text.find("}}", index + 2)
                if close < 0:
                    raise SkillSyntaxError("unterminated '{{' substitution", line=lineno)
                inner = text[index + 2 : close]
                if not inner.startswith(_SLOT_PREFIX):
                    raise SkillSyntaxError(
                        "only {{slot:NAME}} substitutions are allowed", line=lineno
                    )
                name = inner[len(_SLOT_PREFIX) :]
                if not _is_slot_name(name):
                    raise SkillSyntaxError(f"malformed slot reference {inner!r}", line=lineno)
                self.used_slots.append((name, lineno))
                index = close + 2
                continue
            plain.append(text[index])
            index += 1
        residue = "".join(plain)
        for marker in _FORBIDDEN_MARKERS:
            if marker in residue:
                raise SkillSyntaxError(
                    f"{marker!r} is not a valid substitution in a card", line=lineno
                )

    def _check_ws_path(self, value: str, lineno: int, *, as_slot: bool) -> None:
        message: str | None = None
        if not value:
            message = "workspace path must not be empty"
        elif value.startswith("/"):
            message = "workspace path must be relative"
        elif "\\" in value:
            message = "backslashes are forbidden in a workspace path"
        elif ".." in value.split("/"):
            message = "workspace path must not escape the workspace via '..'"
        if message is None:
            return
        if as_slot:
            raise SkillSlotError(message, line=lineno)
        raise SkillSyntaxError(message, line=lineno)

    def _parse_literal(self, text: str, lineno: int) -> CardValue:
        if text.startswith('"'):
            if len(text) < 2 or not text.endswith('"') or '"' in text[1:-1]:
                raise SkillSyntaxError("malformed quoted string literal", line=lineno)
            value = text[1:-1]
            self._scan_value(value, lineno)
            return value
        if text in ("true", "false"):
            return text == "true"
        negative = text.startswith("-")
        digits = text[1:] if negative else text
        if not is_uint(digits):
            raise SkillSyntaxError(f"malformed literal {text!r}", line=lineno)
        return -int(digits) if negative else int(digits)

    def _parse_bool(self, text: str, lineno: int) -> bool:
        if text not in ("true", "false"):
            raise SkillUnknownError(f"expected 'true' or 'false', found {text!r}", line=lineno)
        return text == "true"

    def _parse_uint(self, text: str, lineno: int, what: str) -> int:
        if not is_uint(text):
            raise SkillSyntaxError(f"{what} must be a decimal integer, found {text!r}", line=lineno)
        return int(text)

    # -- header (§3) --------------------------------------------------------

    def _parse_header(self) -> dict[str, str]:
        if not self.body:
            raise SkillVersionError("card has no %ASKILL line", line=1)
        magic = self.body[0]
        if not magic.startswith("%ASKILL "):
            raise SkillVersionError("first line must be '%ASKILL <major>'", line=1)
        major_text = magic[len("%ASKILL ") :]
        if not is_uint(major_text):
            raise SkillVersionError(f"malformed format version {major_text!r}", line=1)
        if int(major_text) != FORMAT_MAJOR:
            raise SkillVersionError(
                f"this parser reads %ASKILL {FORMAT_MAJOR} only, found {major_text}", line=1
            )

        values: dict[str, str] = {}
        for offset, directive in enumerate(_HEADER_DIRECTIVES, start=1):
            lineno = offset + 1
            if offset >= len(self.body) or self.body[offset] == "":
                raise SkillSyntaxError(f"missing header directive {directive}", line=lineno)
            line = self.body[offset]
            name = line.split(" ", 1)[0]
            if name != directive:
                if name.lower() in RESERVED_DIRECTIVES:
                    raise SkillCriteriaError(
                        f"directive {name} declares verification criteria", line=lineno
                    )
                if name.startswith("%") and name not in _KNOWN_DIRECTIVES:
                    raise SkillUnknownError(f"unknown directive {name}", line=lineno)
                raise SkillSyntaxError(f"expected {directive}, found {name!r}", line=lineno)
            values[directive] = self._split_pad(line, directive, lineno)
        return values

    # -- sections -----------------------------------------------------------

    def _section_bounds(self, start: int) -> int:
        index = start
        while index < len(self.body) and self.body[index] != "":
            index += 1
        return index

    def _parse_intent(self, start: int) -> tuple[tuple[str, ...], int]:
        end = self._section_bounds(start)
        if end == start:
            raise SkillSyntaxError("[intent] must carry at least one '> ' line", line=start)
        lines: list[str] = []
        for index in range(start, end):
            scan = _Scan(self.body[index], index + 1)
            scan.literal("> ")
            lines.append(scan.rest())
        return tuple(lines), end

    def _parse_match(self, start: int) -> tuple[tuple[MatchRule, ...], int]:
        end = self._section_bounds(start)
        if end == start:
            raise SkillSyntaxError("[match] must carry at least one rule", line=start)
        rules: list[MatchRule] = []
        for index in range(start, end):
            line = self.body[index]
            lineno = index + 1
            head = line.split(" ", 1)[0]
            _reserved_key_guard(head, lineno)
            try:
                kind = MatchKind(head)
            except ValueError:
                raise SkillUnknownError(f"unknown [match] rule {head!r}", line=lineno) from None
            value = self._split_pad(line, head, lineno)
            if kind is MatchKind.KEYWORD:
                rules.append(MatchRule(kind, self._parse_keywords(value, lineno)))
            elif kind is MatchKind.PATH_PREFIX:
                self._scan_value(value, lineno)
                self._check_ws_path(value, lineno, as_slot=False)
                rules.append(MatchRule(kind, (value,)))
            else:
                if not _is_tool_name(value):
                    raise SkillSyntaxError(f"malformed tool name {value!r}", line=lineno)
                rules.append(MatchRule(kind, (value,)))
        return tuple(rules), end

    def _parse_keywords(self, value: str, lineno: int) -> tuple[str, ...]:
        parts = value.split("|")
        keywords: list[str] = []
        for position, part in enumerate(parts):
            token = part
            if position > 0:
                if not token.startswith(" "):
                    raise SkillSyntaxError("'|' must be surrounded by single spaces", line=lineno)
                token = token[1:]
            if position < len(parts) - 1:
                if not token.endswith(" "):
                    raise SkillSyntaxError("'|' must be surrounded by single spaces", line=lineno)
                token = token[:-1]
            if not token or token != token.strip(" "):
                raise SkillSyntaxError("keyword must not be empty or padded", line=lineno)
            self._scan_value(token, lineno)
            keywords.append(token)
        return tuple(keywords)

    def _parse_require(self, start: int) -> tuple[Require, int]:
        end = self._section_bounds(start)
        if end == start:
            raise SkillSyntaxError("[require] must carry at least one line", line=start)
        limits: dict[str, int] = {}
        states: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for index in range(start, end):
            line = self.body[index]
            lineno = index + 1
            head = line.split(" ", 1)[0]
            _reserved_key_guard(head, lineno)
            if head not in ("limit", "state"):
                raise SkillUnknownError(f"unknown [require] line {head!r}", line=lineno)
            body = self._split_pad(line, head, lineno)
            key, separator, raw = body.partition(" = ")
            if not separator:
                raise SkillSyntaxError("expected '<key> = <value>'", line=lineno)
            _reserved_key_guard(key, lineno)
            if key in seen:
                raise SkillSyntaxError(f"duplicate [require] key {key!r}", line=lineno)
            seen.add(key)
            if head == "limit":
                if key not in _LIMIT_NAMES:
                    raise SkillUnknownError(f"unknown limit {key!r}", line=lineno)
                limits[key] = self._parse_uint(raw, lineno, "limit value")
            else:
                if not _is_state_key(key):
                    raise SkillSyntaxError(f"malformed state key {key!r}", line=lineno)
                states.append((key, self._parse_bool(raw, lineno)))
        require = Require(
            max_steps=limits.get("max-steps"),
            max_bytes=limits.get("max-bytes"),
            states=tuple(states),
        )
        return require, end

    def _parse_slots(self, start: int) -> tuple[tuple[Slot, ...], int]:
        end = self._section_bounds(start)
        if end == start:
            raise SkillSyntaxError("[slots] must declare at least one slot", line=start)
        if end - start > MAX_SLOTS:
            raise SkillLimitError(
                f"card declares {end - start} slots, limit is {MAX_SLOTS}",
                line=start + MAX_SLOTS + 1,
            )
        slots: list[Slot] = []
        for index in range(start, end):
            lineno = index + 1
            scan = _Scan(self.body[index], lineno)
            name = scan.word()
            scan.pad()
            scan.literal(":")
            scan.sp()
            type_text = scan.word()
            scan.pad()
            presence = scan.word()
            default_text: str | None = None
            if not scan.at_end():
                scan.pad()
                scan.literal("default")
                scan.sp()
                scan.literal("=")
                scan.sp()
                default_text = scan.rest()
            _reserved_key_guard(name, lineno)
            if not _is_slot_name(name):
                raise SkillSyntaxError(f"malformed slot name {name!r}", line=lineno)
            if name in self.declared:
                raise SkillSyntaxError(f"duplicate slot {name!r}", line=lineno)
            try:
                slot_type = SlotType(type_text)
            except ValueError:
                raise SkillUnknownError(f"unknown slot type {type_text!r}", line=lineno) from None
            if presence not in ("required", "optional"):
                raise SkillUnknownError(f"unknown slot presence {presence!r}", line=lineno)
            default = self._parse_default(default_text, slot_type, lineno)
            slot = Slot(name=name, type=slot_type, required=presence == "required", default=default)
            self.declared[name] = slot
            self.slot_lines[name] = lineno
            slots.append(slot)
        return tuple(slots), end

    def _parse_default(
        self, text: str | None, slot_type: SlotType, lineno: int
    ) -> CardValue | None:
        if text is None:
            return None
        for marker in _FORBIDDEN_MARKERS:
            if marker in text:
                raise SkillSyntaxError(
                    "a slot default may not contain a nested substitution", line=lineno
                )
        value = self._parse_literal(text, lineno)
        if slot_type in (SlotType.PATH, SlotType.TEXT):
            if not isinstance(value, str):
                raise SkillSlotError(
                    f"slot of type {slot_type} needs a quoted default", line=lineno
                )
            if slot_type is SlotType.PATH:
                self._check_ws_path(value, lineno, as_slot=True)
        elif slot_type is SlotType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SkillSlotError("slot of type int needs an integer default", line=lineno)
        elif not isinstance(value, bool):
            raise SkillSlotError("slot of type bool needs a true/false default", line=lineno)
        return value

    def _read_heredoc(self, start: int) -> tuple[HeredocText, int]:
        index = start
        lines: list[str] = []
        while index < len(self.body):
            raw = self.body[index]
            if raw == _HEREDOC_CLOSE:
                return HeredocText(tuple(lines)), index + 1
            if raw == "":
                lines.append("")
            elif raw.startswith(_HEREDOC_INDENT):
                content = raw[len(_HEREDOC_INDENT) :]
                self._scan_value(content, index + 1)
                self.plan_text.append((content, index + 1))
                lines.append(content)
            else:
                raise SkillSyntaxError(
                    f"unterminated heredoc opened on line {start}: a body line must be indented "
                    f"by exactly 4 spaces and the block must close with '  >>'",
                    line=index + 1,
                )
            index += 1
        raise SkillSyntaxError(
            f"unterminated heredoc opened on line {start}: '<<' without a closing '  >>'",
            line=start,
        )

    def _parse_plan(self, start: int) -> tuple[tuple[PlanStep, ...], int]:
        if start >= len(self.body) or self.body[start] == "":
            raise SkillSyntaxError("[plan] must declare at least one step", line=start)
        steps: list[PlanStep] = []
        index = start
        while index < len(self.body) and self.body[index] != "":
            lineno = index + 1
            line = self.body[index]
            if line.startswith(_ARG_PREFIX):
                raise SkillSyntaxError("argument line before any step", line=lineno)
            scan = _Scan(line, lineno)
            number_text = scan.word()
            scan.sp()
            scan.literal("tool")
            scan.sp()
            tool = scan.word()
            scan.done()
            number = self._parse_uint(number_text, lineno, "step number")
            if number != len(steps) + 1:
                raise SkillOrderError(
                    f"step numbering must be sequential: expected {len(steps) + 1}, found {number}",
                    line=lineno,
                )
            if number > MAX_PLAN_STEPS:
                raise SkillLimitError(
                    f"card declares more than {MAX_PLAN_STEPS} plan steps", line=lineno
                )
            if not _is_tool_name(tool):
                raise SkillSyntaxError(f"malformed tool name {tool!r}", line=lineno)
            index += 1
            args, index = self._parse_step_args(index)
            steps.append(PlanStep(number=number, tool=tool, args=args))
        return tuple(steps), index

    def _parse_step_args(self, start: int) -> tuple[tuple[tuple[str, CardValue], ...], int]:
        args: list[tuple[str, CardValue]] = []
        keys: set[str] = set()
        index = start
        while index < len(self.body) and self.body[index].startswith(_ARG_PREFIX):
            lineno = index + 1
            scan = _Scan(self.body[index], lineno)
            scan.literal(_ARG_PREFIX)
            key = scan.word()
            scan.sp()
            scan.literal("=")
            scan.sp()
            raw = scan.rest()
            _reserved_key_guard(key, lineno)
            if not _is_slot_name(key):
                raise SkillSyntaxError(f"malformed argument key {key!r}", line=lineno)
            if key in keys:
                raise SkillSyntaxError(f"duplicate argument key {key!r} in one step", line=lineno)
            keys.add(key)
            value: CardValue
            if raw == _HEREDOC_OPEN:
                value, index = self._read_heredoc(index + 1)
            else:
                value = self._parse_literal(raw, lineno)
                if isinstance(value, str):
                    self.plan_text.append((value, lineno))
                index += 1
            args.append((key, value))
        return tuple(args), index

    def _parse_claims(self, start: int) -> tuple[tuple[Claim, ...], int]:
        end = self._section_bounds(start)
        if end == start:
            raise SkillSyntaxError("[claims] must carry at least one claim", line=start)
        claims: list[Claim] = []
        seen: set[str] = set()
        for index in range(start, end):
            line = self.body[index]
            lineno = index + 1
            head = line.split(" ", 1)[0]
            _reserved_key_guard(head, lineno)
            try:
                kind = ClaimKind(head)
            except ValueError:
                raise SkillUnknownError(f"unknown claim {head!r}", line=lineno) from None
            if head in seen:
                raise SkillSyntaxError(f"duplicate claim {head!r}", line=lineno)
            seen.add(head)
            value = self._split_pad(line, head, lineno)
            if kind is ClaimKind.PRODUCES_FILE:
                self._scan_value(value, lineno)
                self._check_ws_path(value, lineno, as_slot=False)
                claims.append(Claim(kind, value))
            else:
                claims.append(Claim(kind, self._parse_bool(value, lineno)))
        return tuple(claims), end

    def _parse_origin(self, start: int) -> tuple[Origin, int]:
        end = self._section_bounds(start)
        values: dict[str, str] = {}
        for position, key in enumerate(_ORIGIN_KEYS):
            index = start + position
            lineno = index + 1
            if index >= end:
                raise SkillSyntaxError(f"[origin] is missing key {key!r}", line=lineno)
            line = self.body[index]
            head = line.split(" ", 1)[0]
            _reserved_key_guard(head, lineno)
            if head != key:
                if head not in _ORIGIN_KEYS:
                    raise SkillUnknownError(f"unknown [origin] key {head!r}", line=lineno)
                raise SkillSyntaxError(f"expected [origin] key {key!r}, found {head!r}", line=lineno)
            values[key] = self._split_pad(line, key, lineno)
            if key == "trust-at-capture":
                self.trust_line = lineno
        if end != start + len(_ORIGIN_KEYS):
            raise SkillSyntaxError(
                "[origin] carries unexpected extra lines", line=start + len(_ORIGIN_KEYS) + 1
            )

        flow_line = start + 1
        flow = values["flow"]
        if not _is_owner_id(flow):
            raise SkillSyntaxError(f"malformed flow id {flow!r}", line=flow_line)
        steps = self._parse_uint(values["steps"], start + 2, "[origin] steps")
        captured = _parse_timestamp(values["captured"], start + 3)
        try:
            verdict = Verdict(values["verdict"])
        except ValueError:
            raise SkillUnknownError(
                f"unknown [origin] verdict {values['verdict']!r}", line=start + 4
            ) from None
        try:
            trust = Trust(values["trust-at-capture"])
        except ValueError:
            raise SkillUnknownError(
                f"unknown trust value {values['trust-at-capture']!r}", line=start + 5
            ) from None
        origin = Origin(
            flow=flow, steps=steps, captured=captured, verdict=verdict, trust_at_capture=trust
        )
        return origin, end

    # -- driver -------------------------------------------------------------

    def parse(self) -> SkillCard:
        header = self._parse_header()
        header_end = len(_HEADER_DIRECTIVES) + 1

        intent: tuple[str, ...] = ()
        match_mode: MatchMode | None = None
        match: tuple[MatchRule, ...] = ()
        require: Require | None = None
        require_line = 0
        slots: tuple[Slot, ...] = ()
        plan: tuple[PlanStep, ...] = ()
        claims: tuple[Claim, ...] = ()
        origin: Origin | None = None

        seen: set[str] = set()
        highest = -1
        index = header_end
        while index < len(self.body):
            if self.body[index] != "":
                raise SkillSyntaxError("expected a blank line between blocks", line=index + 1)
            index += 1
            if index >= len(self.body):
                raise SkillSyntaxError("card ends with a stray blank line", line=index)
            name, tail, lineno = self._read_section_head(index)
            if name in seen:
                raise SkillOrderError(f"duplicate section [{name}]", line=lineno)
            rank = SECTION_ORDER.index(name)
            if rank < highest:
                raise SkillOrderError(f"section [{name}] is out of order", line=lineno)
            seen.add(name)
            highest = rank
            if name != "match" and tail:
                raise SkillSyntaxError(f"unexpected text after [{name}]", line=lineno)
            index += 1
            if name == "intent":
                intent, index = self._parse_intent(index)
            elif name == "match":
                match_mode = _parse_match_mode(tail, lineno)
                match, index = self._parse_match(index)
            elif name == "require":
                require_line = lineno
                require, index = self._parse_require(index)
            elif name == "slots":
                slots, index = self._parse_slots(index)
            elif name == "plan":
                plan, index = self._parse_plan(index)
            elif name == "claims":
                claims, index = self._parse_claims(index)
            else:
                origin, index = self._parse_origin(index)

        missing = REQUIRED_SECTIONS - seen
        if missing:
            listed = ", ".join(f"[{name}]" for name in SECTION_ORDER if name in missing)
            raise SkillOrderError(f"card is missing mandatory section(s): {listed}")
        if origin is None:  # pragma: no cover - the mandatory-section check already covers it
            raise SkillOrderError("card is missing mandatory section [origin]")

        try:
            trust = Trust(header["%trust"])
        except ValueError:
            raise SkillUnknownError(f"unknown %trust value {header['%trust']!r}", line=6) from None
        try:
            risk = RiskCeiling(header["%risk"])
        except ValueError:
            raise SkillUnknownError(f"unknown %risk value {header['%risk']!r}", line=7) from None
        self._validate_semantics(trust, risk, origin, plan, require, require_line)

        skill_id = header["%id"]
        if not _is_skill_id(skill_id):
            raise SkillSyntaxError(f"%id must be 'skl-<uuid>', found {skill_id!r}", line=2)
        slug = header["%slug"]
        if not _is_slug(slug):
            raise SkillSyntaxError(f"malformed %slug {slug!r}", line=3)
        owner = header["%owner"]
        if not _is_owner_id(owner):
            raise SkillSyntaxError(f"malformed %owner {owner!r}", line=5)

        return SkillCard(
            skill_id=skill_id,
            slug=slug,
            version=self._parse_uint(header["%version"], 4, "%version"),
            owner_id=owner,
            trust=trust,
            risk=risk,
            intent=intent,
            plan=plan,
            origin=origin,
            format_version=FORMAT_MAJOR,
            match_mode=match_mode,
            match=match,
            require=require,
            slots=slots,
            claims=claims,
        )

    def _read_section_head(self, index: int) -> tuple[str, str, int]:
        line = self.body[index]
        lineno = index + 1
        if not line.startswith("["):
            raise SkillSyntaxError("expected a section header", line=lineno)
        close = line.find("]")
        if close < 0:
            raise SkillSyntaxError("section header is missing ']'", line=lineno)
        name = line[1:close]
        if name.lower() in RESERVED_SECTIONS:
            raise SkillCriteriaError(
                f"section [{name}] declares verification criteria, which a card may never carry",
                line=lineno,
            )
        if name not in SECTION_ORDER:
            raise SkillUnknownError(f"unknown section [{name}]", line=lineno)
        return name, line[close + 1 :], lineno

    def _validate_semantics(
        self,
        trust: Trust,
        risk: RiskCeiling,
        origin: Origin,
        plan: tuple[PlanStep, ...],
        require: Require | None,
        require_line: int,
    ) -> None:
        for name, lineno in self.used_slots:
            if name not in self.declared:
                raise SkillSlotError(f"{{{{slot:{name}}}}} is not declared in [slots]", line=lineno)
        for name, lineno in self.slot_lines.items():
            if all(used != name for used, _ in self.used_slots):
                raise SkillSlotError(f"slot {name!r} is declared but never used", line=lineno)
        if origin.trust_at_capture is not trust:
            raise SkillSyntaxError(
                f"[origin] trust-at-capture is {origin.trust_at_capture.value!r} "
                f"but %trust is {trust.value!r}",
                line=self.trust_line,
            )
        if trust is Trust.UNTRUSTED:
            if risk is not RiskCeiling.LOW:
                raise SkillSyntaxError("%risk must be LOW for an untrusted card", line=7)
            for text, lineno in self.plan_text:
                if "://" in text:
                    raise SkillSyntaxError(
                        "an untrusted card may not carry a URL in [plan]", line=lineno
                    )
        if require is not None and require.max_steps is not None:
            if require.max_steps > MAX_PLAN_STEPS:
                raise SkillLimitError(
                    f"require.max-steps={require.max_steps} exceeds the format limit "
                    f"of {MAX_PLAN_STEPS}",
                    line=require_line,
                )
            if require.max_steps < len(plan):
                raise SkillLimitError(
                    f"require.max-steps={require.max_steps} is below the {len(plan)} declared steps",
                    line=require_line,
                )


def _parse_match_mode(tail: str, lineno: int) -> MatchMode:
    if not tail.startswith(" mode="):
        raise SkillSyntaxError("[match] must declare ' mode=all' or ' mode=any'", line=lineno)
    text = tail[len(" mode=") :]
    try:
        return MatchMode(text)
    except ValueError:
        raise SkillUnknownError(f"unknown [match] mode {text!r}", line=lineno) from None


def _parse_timestamp(text: str, lineno: int) -> datetime:
    try:
        stamp = datetime.strptime(text, _TIMESTAMP_FORMAT)
    except ValueError:
        raise SkillSyntaxError(
            f"[origin] captured must be RFC 3339 UTC (…Z), found {text!r}", line=lineno
        ) from None
    return stamp.replace(tzinfo=UTC)


def parse_card(data: bytes) -> SkillCard:
    """Parse raw ``ASKILL/1`` bytes into a :class:`SkillCard`, fail-closed on the first error.

    Order of gates follows the spec: size and encoding (§2, §5), then footer integrity
    (§6.3 — "integrity first, parsing second"), then the grammar itself (§3).
    """
    text = _decode(data)
    if not text.endswith("\n"):
        raise SkillSyntaxError("card file must end with a LF")
    lines = text.split("\n")[:-1]
    _scan_encoding(lines)
    verify_footer(data)
    if len(lines) < 3 or lines[-2] != "":
        raise SkillSyntaxError(
            "the %end footer must be preceded by exactly one blank line", line=len(lines)
        )
    return _Parser(lines[:-2]).parse()


# --- serializer (§4) -------------------------------------------------------------


def _render_literal(value: CardValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    raise TypeError("heredoc values are rendered by the plan writer")


def _render_plain(value: CardValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError("a claim value is never a heredoc")


def _render_sections(card: SkillCard) -> list[list[str]]:
    blocks: list[list[str]] = []

    blocks.append(["[intent]", *(f"> {line}" for line in card.intent)])

    if card.match_mode is not None:
        match_block = [f"[match] mode={card.match_mode.value}"]
        for rule in card.match:
            match_block.append(_field(rule.kind.value, _W_MATCH) + " | ".join(rule.values))
        blocks.append(match_block)

    if card.require is not None:
        require_block = ["[require]"]
        if card.require.max_steps is not None:
            require_block.append(_field("limit", _W_REQUIRE) + f"max-steps = {card.require.max_steps}")
        if card.require.max_bytes is not None:
            require_block.append(_field("limit", _W_REQUIRE) + f"max-bytes = {card.require.max_bytes}")
        for key, state in card.require.states:
            rendered = "true" if state else "false"
            require_block.append(_field("state", _W_REQUIRE) + f"{key} = {rendered}")
        blocks.append(require_block)

    if card.slots:
        slot_block = ["[slots]"]
        for slot in card.slots:
            presence = "required" if slot.required else "optional"
            head = _field(slot.name, _W_SLOT_NAME) + ": " + _field(slot.type.value, _W_SLOT_TYPE)
            if slot.default is None:
                slot_block.append(head + presence)
            else:
                default = _render_literal(slot.default)
                slot_block.append(
                    head + _field(presence, _W_SLOT_PRESENCE) + f"default = {default}"
                )
        blocks.append(slot_block)

    plan_block = ["[plan]"]
    for step in card.plan:
        plan_block.append(f"{step.number} tool {step.tool}")
        for key, value in step.args:
            if isinstance(value, HeredocText):
                plan_block.append(f"{_ARG_PREFIX}{key} = {_HEREDOC_OPEN}")
                plan_block.extend(
                    "" if line == "" else _HEREDOC_INDENT + line for line in value.lines
                )
                plan_block.append(_HEREDOC_CLOSE)
            else:
                plan_block.append(f"{_ARG_PREFIX}{key} = {_render_literal(value)}")
    blocks.append(plan_block)

    if card.claims:
        claim_block = ["[claims]"]
        for claim in card.claims:
            claim_block.append(_field(claim.kind.value, _W_CLAIM) + _render_plain(claim.value))
        blocks.append(claim_block)

    origin = card.origin
    captured = origin.captured
    if captured.tzinfo is not None:
        captured = captured.astimezone(UTC)
    blocks.append(
        [
            "[origin]",
            _field("flow", _W_ORIGIN) + origin.flow,
            _field("steps", _W_ORIGIN) + str(origin.steps),
            _field("captured", _W_ORIGIN) + captured.strftime(_TIMESTAMP_FORMAT),
            _field("verdict", _W_ORIGIN) + origin.verdict.value,
            _field("trust-at-capture", _W_ORIGIN) + origin.trust_at_capture.value,
        ]
    )
    return blocks


def render_card(card: SkillCard) -> bytes:
    """Serialize a card to its canonical bytes, footer included (§4, §6.2)."""
    header = [
        f"%ASKILL {card.format_version}",
        _field("%id", _W_HEADER) + card.skill_id,
        _field("%slug", _W_HEADER) + card.slug,
        _field("%version", _W_HEADER) + str(card.version),
        _field("%owner", _W_HEADER) + card.owner_id,
        _field("%trust", _W_HEADER) + card.trust.value,
        _field("%risk", _W_HEADER) + card.risk.value,
    ]
    blocks = [header, *_render_sections(card)]
    head = ("\n\n".join("\n".join(block) for block in blocks) + "\n\n").encode()
    return head + footer_line(head)
