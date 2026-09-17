"""P2.1.e — ``ASKILL/1`` parser, serializer and canonicaliser.

Every negative test asserts the normative error **code** from docs/SKILL_FORMAT.md §8,
not merely that something was raised: a fail-closed format is only useful if the reason
for the refusal is the documented one.
"""

from __future__ import annotations

import pathlib

import pytest

from antigona.skills import (
    HeredocText,
    MatchMode,
    RiskCeiling,
    SkillFormatError,
    SkillState,
    SlotType,
    Trust,
    body_digest,
    canonicalize,
    footer_line,
    parse_card,
    render_card,
    verify_footer,
)

EXAMPLES = pathlib.Path(__file__).resolve().parents[2] / "examples" / "skills"

BASE = """\
%ASKILL 1
%id       skl-4d1a6f92-3c07-4b58-8e21-7a0b5c9d3e64
%slug     minimal-probe
%version  1
%owner    owner-42
%trust    trusted
%risk     LOW

[intent]
> Минимальная карточка для негативных проб парсера.

[plan]
1 tool workspace.read_text
  ~ path = "notes/probe.md"

[origin]
flow              flow-00001
steps             1
captured          2026-07-26T08:00:00Z
verdict           verifier-pass
trust-at-capture  trusted
"""


def seal(body: str) -> bytes:
    """Append a correctly computed ``%end`` footer (§6.2) to a card body."""
    head = body.encode() + b"\n"
    return head + footer_line(head)


def swap(old: str, new: str, body: str = BASE) -> str:
    assert old in body
    return body.replace(old, new, 1)


def code_of(data: bytes) -> str:
    with pytest.raises(SkillFormatError) as excinfo:
        parse_card(data)
    return excinfo.value.code


# --- happy path ------------------------------------------------------------------


def test_parses_the_full_example_card() -> None:
    data = (EXAMPLES / "workspace-report-scaffold.askill").read_bytes()
    card = parse_card(data)

    assert card.format_version == 1
    assert card.slug == "workspace-report-scaffold"
    assert card.version == 3
    assert card.owner_id == "owner-42"
    assert card.trust is Trust.TRUSTED
    assert card.risk is RiskCeiling.MEDIUM
    assert len(card.intent) == 2
    assert card.match_mode is MatchMode.ALL
    assert [rule.values for rule in card.match] == [
        ("отчёт", "report", "сводка"),
        ("reports/",),
        ("workspace.write_text",),
    ]
    assert card.require is not None
    assert (card.require.max_steps, card.require.max_bytes) == (6, 262144)
    assert card.require.states == (("workspace.writable", True),)
    assert [(slot.name, slot.type, slot.required) for slot in card.slots] == [
        ("report_dir", SlotType.PATH, True),
        ("title", SlotType.TEXT, False),
    ]
    assert [step.tool for step in card.plan] == [
        "workspace.mkdir",
        "workspace.write_text",
        "workspace.read_text",
    ]
    assert card.plan[1].args[1] == (
        "content",
        HeredocText(("# {{slot:title}}", "Раздел заполняется исполнителем шага.")),
    )
    assert card.origin.flow == "flow-12345"
    assert card.origin.captured.isoformat() == "2026-07-26T10:12:00+00:00"


def test_roundtrip_is_idempotent() -> None:
    for path in sorted(EXAMPLES.glob("*.askill")):
        data = path.read_bytes()

        # render(parse(x)) == canonicalize(x), and the shipped examples are canonical.
        assert render_card(parse_card(data)) == canonicalize(data), path.name
        assert canonicalize(data) == data, path.name

        # A second pass changes nothing.
        once = canonicalize(data)
        assert canonicalize(once) == once, path.name
        assert render_card(parse_card(once)) == once, path.name


def test_non_canonical_input_parses_and_canonicalises() -> None:
    loose = seal(
        swap("%slug     minimal-probe", "%slug          minimal-probe").replace(
            "flow              flow-00001", "flow   flow-00001"
        )
    )
    tight = seal(BASE)

    assert loose != tight
    assert canonicalize(loose) == tight
    assert render_card(parse_card(loose)) == tight


def test_footer_covers_the_body_up_to_the_blank_line_before_end() -> None:
    data = seal(BASE)
    digest, count = body_digest(data)

    assert verify_footer(data) == (digest, count)
    # bytes= is exactly the offset of the first byte of the %end line (§6.1).
    assert data[count : count + 4] == b"%end"
    assert f"%end sha256:{digest} bytes={count}\n".encode() == data[count:]


def test_untrusted_example_keeps_a_low_ceiling() -> None:
    card = parse_card((EXAMPLES / "artifact-digest-manifest.askill").read_bytes())

    assert card.trust is Trust.UNTRUSTED
    assert card.risk is RiskCeiling.LOW
    assert card.origin.trust_at_capture is Trust.UNTRUSTED
    assert card.require is None


# --- fail-closed on directives and sections --------------------------------------


def test_rejects_unknown_directive() -> None:
    assert code_of(seal(swap("%version  1\n", "%hint     подсказка\n%version  1\n"))) == "E-UNKNOWN"
    assert code_of(seal(swap("%risk     LOW\n", "%risk     LOW\n%extra    x\n"))) == "E-SYNTAX"
    assert code_of((EXAMPLES / "invalid" / "unknown-directive.askill").read_bytes()) == "E-UNKNOWN"


def test_rejects_unknown_section() -> None:
    assert code_of(seal(swap("[plan]\n", "[notes]\n> посторонняя\n\n[plan]\n"))) == "E-UNKNOWN"
    assert code_of((EXAMPLES / "invalid" / "unknown-section.askill").read_bytes()) == "E-UNKNOWN"


def test_rejects_duplicate_and_misordered_sections() -> None:
    duplicate = seal(swap("[origin]\n", "[intent]\n> дубль\n\n[origin]\n"))
    assert code_of(duplicate) == "E-ORDER"

    misordered = seal(
        swap(
            "[intent]\n> Минимальная карточка для негативных проб парсера.\n\n[plan]\n"
            '1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n',
            "[plan]\n1 tool workspace.read_text\n  ~ path = \"notes/probe.md\"\n\n"
            "[intent]\n> секция после [plan]\n",
        )
    )
    assert code_of(misordered) == "E-ORDER"

    missing = seal(
        swap(
            "\n[origin]\nflow              flow-00001\nsteps             1\n"
            "captured          2026-07-26T08:00:00Z\nverdict           verifier-pass\n"
            "trust-at-capture  trusted\n",
            "",
        )
    )
    assert code_of(missing) == "E-ORDER"

    gap = seal(
        swap(
            '1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n',
            '1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n'
            '3 tool workspace.read_text\n  ~ path = "notes/other.md"\n',
        )
    )
    assert code_of(gap) == "E-ORDER"

    for name in ("duplicate-section.askill", "misordered-sections.askill"):
        assert code_of((EXAMPLES / "invalid" / name).read_bytes()) == "E-ORDER"


# --- encoding and limits ---------------------------------------------------------


def test_rejects_crlf_bom_and_oversized_line() -> None:
    valid = seal(BASE)

    assert code_of(valid.replace(b"\n", b"\r\n")) == "E-ENC"
    assert code_of(b"\xef\xbb\xbf" + valid) == "E-ENC"
    assert code_of(valid.replace(b"%id       ", b"%id\t      ")) == "E-ENC"
    assert code_of(valid.replace(b"probe.md", b"probe\x07.md")) == "E-ENC"
    assert code_of(b"\xff\xfe" + valid) == "E-ENC"

    oversized = seal(swap("> Минимальная карточка для негативных проб парсера.\n", "> " + "x" * 220))
    assert code_of(oversized) == "E-LIMIT"

    assert code_of((EXAMPLES / "invalid" / "crlf-line-endings.askill").read_bytes()) == "E-ENC"
    assert code_of((EXAMPLES / "invalid" / "oversized-line.askill").read_bytes()) == "E-LIMIT"


def test_rejects_file_over_size_limit() -> None:
    filler = "".join(f"> строка описания номер {index}\n" for index in range(4000))
    oversized = seal(swap("> Минимальная карточка для негативных проб парсера.\n", filler))

    assert len(oversized) > 65536
    assert code_of(oversized) == "E-LIMIT"


def test_rejects_too_many_steps_and_slots() -> None:
    steps = "".join(
        f'{number} tool workspace.read_text\n  ~ path = "notes/probe.md"\n'
        for number in range(1, 34)
    )
    too_many_steps = seal(
        swap('1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n', steps)
    )
    assert code_of(too_many_steps) == "E-LIMIT"

    declarations = "".join(
        f"s{index:<11}: text   optional\n" for index in range(17)
    )
    uses = "".join(f'  ~ a{index} = "{{{{slot:s{index}}}}}"\n' for index in range(17))
    too_many_slots = seal(
        swap(
            '[plan]\n1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n',
            f"[slots]\n{declarations}\n[plan]\n1 tool workspace.read_text\n{uses}",
        )
    )
    assert code_of(too_many_slots) == "E-LIMIT"


def test_rejects_require_max_steps_outside_the_format_limits() -> None:
    exact = seal(swap("\n[plan]\n", "\n[require]\nlimit  max-steps = 1\n\n[plan]\n"))
    card = parse_card(exact)
    assert card.require is not None and card.require.max_steps == 1
    assert render_card(card) == exact

    too_small = seal(
        swap("\n[plan]\n", "\n[require]\nlimit  max-steps = 0\n\n[plan]\n")
    )
    assert code_of(too_small) == "E-LIMIT"

    too_large = seal(
        swap("\n[plan]\n", "\n[require]\nlimit  max-steps = 64\n\n[plan]\n")
    )
    assert code_of(too_large) == "E-LIMIT"


# --- grammar ---------------------------------------------------------------------


def test_rejects_unterminated_heredoc() -> None:
    runs_into_the_next_section = seal(
        swap(
            '  ~ path = "notes/probe.md"\n',
            '  ~ path = "notes/probe.md"\n  ~ content = <<\n    первая строка\n',
        )
    )
    assert code_of(runs_into_the_next_section) == "E-SYNTAX"

    runs_to_end_of_file = seal(
        swap(
            "\n[origin]\nflow              flow-00001\nsteps             1\n"
            "captured          2026-07-26T08:00:00Z\nverdict           verifier-pass\n"
            "trust-at-capture  trusted\n",
            "",
            swap(
                '  ~ path = "notes/probe.md"\n',
                '  ~ path = "notes/probe.md"\n  ~ content = <<\n    первая строка\n',
            ),
        )
    )
    assert code_of(runs_to_end_of_file) == "E-SYNTAX"

    with pytest.raises(SkillFormatError) as excinfo:
        parse_card(runs_into_the_next_section)
    assert excinfo.value.line is not None
    assert "heredoc" in excinfo.value.message

    assert code_of((EXAMPLES / "invalid" / "unterminated-heredoc.askill").read_bytes()) == "E-SYNTAX"


def test_accepts_a_blank_line_inside_a_heredoc_only() -> None:
    with_blank = seal(
        swap(
            '  ~ path = "notes/probe.md"\n',
            '  ~ path = "notes/probe.md"\n  ~ content = <<\n    первая\n\n    вторая\n  >>\n',
        )
    )
    card = parse_card(with_blank)
    assert card.plan[0].args[1] == ("content", HeredocText(("первая", "", "вторая")))
    assert render_card(card) == with_blank

    stray_blank = seal(swap("[plan]\n1 tool", "[plan]\n\n1 tool"))
    assert code_of(stray_blank) == "E-SYNTAX"


def test_rejects_trailing_whitespace_and_missing_final_lf() -> None:
    assert code_of(seal(BASE).replace(b"%risk     LOW\n", b"%risk     LOW \n")) == "E-SYNTAX"
    assert code_of(seal(BASE).rstrip(b"\n")) == "E-SYNTAX"


def test_rejects_a_future_format_version() -> None:
    assert code_of(seal(swap("%ASKILL 1\n", "%ASKILL 2\n"))) == "E-VERSION"
    assert code_of(seal(swap("%ASKILL 1\n", "%ASKILL x\n"))) == "E-VERSION"
    assert code_of(seal(swap("%ASKILL 1\n", "[intent]\n"))) == "E-VERSION"
    assert code_of((EXAMPLES / "invalid" / "future-version.askill").read_bytes()) == "E-VERSION"


def test_rejects_duplicate_keys() -> None:
    duplicate_arg = seal(
        swap(
            '  ~ path = "notes/probe.md"\n',
            '  ~ path = "notes/probe.md"\n  ~ path = "notes/other.md"\n',
        )
    )
    assert code_of(duplicate_arg) == "E-SYNTAX"

    duplicate_claim = seal(
        swap("\n[origin]\n", "\n[claims]\nno-network         true\nno-network         false\n\n[origin]\n")
    )
    assert code_of(duplicate_claim) == "E-SYNTAX"


def test_rejects_inconsistent_trust_and_risk() -> None:
    mismatch = seal(swap("trust-at-capture  trusted", "trust-at-capture  untrusted"))
    assert code_of(mismatch) == "E-SYNTAX"

    untrusted_high = seal(
        swap("%trust    trusted", "%trust    untrusted")
        .replace("%risk     LOW", "%risk     HIGH")
        .replace("trust-at-capture  trusted", "trust-at-capture  untrusted")
    )
    assert code_of(untrusted_high) == "E-SYNTAX"


def test_untrusted_card_may_not_carry_a_url_in_the_plan() -> None:
    with_url = seal(
        swap("%trust    trusted", "%trust    untrusted")
        .replace("trust-at-capture  trusted", "trust-at-capture  untrusted")
        .replace('  ~ path = "notes/probe.md"', '  ~ source = "https://example.invalid/x"')
    )
    assert code_of(with_url) == "E-SYNTAX"


# --- slots -----------------------------------------------------------------------


def test_rejects_undeclared_slot_reference() -> None:
    undeclared = seal(swap('  ~ path = "notes/probe.md"', '  ~ path = "{{slot:missing}}/probe.md"'))
    assert code_of(undeclared) == "E-SLOT"

    unused = seal(
        swap(
            "\n[plan]\n",
            '\n[slots]\nunused       : text   optional  default = "x"\n\n[plan]\n',
        )
    )
    assert code_of(unused) == "E-SLOT"

    assert code_of((EXAMPLES / "invalid" / "undeclared-slot.askill").read_bytes()) == "E-SLOT"


def test_rejects_slot_type_mismatch() -> None:
    cases = {
        "retries      : int    optional  default = \"three\"": "E-SLOT",
        "retries      : int    optional  default = true": "E-SLOT",
        "retries      : bool   optional  default = 3": "E-SLOT",
        "retries      : text   optional  default = 3": "E-SLOT",
        "retries      : path   optional  default = \"../outside\"": "E-SLOT",
        "retries      : nope   optional": "E-UNKNOWN",
    }
    for declaration, expected in cases.items():
        card = seal(
            swap(
                '\n[plan]\n1 tool workspace.read_text\n  ~ path = "notes/probe.md"\n',
                f"\n[slots]\n{declaration}\n\n[plan]\n1 tool workspace.read_text\n"
                '  ~ path = "{{slot:retries}}"\n',
            )
        )
        assert code_of(card) == expected, declaration

    assert code_of((EXAMPLES / "invalid" / "slot-type-mismatch.askill").read_bytes()) == "E-SLOT"


def test_rejects_substitutions_that_are_not_slot_references() -> None:
    for value in ('"${HOME}/x"', '"$(whoami)"', '"{{env:HOME}}"', '"{{slot:report_dir"'):
        card = seal(swap('  ~ path = "notes/probe.md"', f"  ~ path = {value}"))
        assert code_of(card) == "E-SYNTAX", value


# --- integrity -------------------------------------------------------------------


def test_rejects_footer_hash_mismatch() -> None:
    valid = seal(BASE)

    tampered = valid.replace(b"probe.md", b"probe.MD")
    assert len(tampered) == len(valid)
    assert code_of(tampered) == "E-HASH"

    # A one-byte change of the declared digest is just as fatal.
    digest, count = body_digest(valid)
    flipped = valid.replace(digest.encode(), (("0" if digest[0] != "0" else "1") + digest[1:]).encode())
    assert code_of(flipped) == "E-HASH"

    wrong_count = valid.replace(f"bytes={count}".encode(), f"bytes={count + 1}".encode())
    assert code_of(wrong_count) == "E-HASH"

    # A malformed footer is a grammar failure, not an integrity failure.
    assert code_of(valid.replace(f"sha256:{digest}".encode(), b"sha256:0000")) == "E-SYNTAX"
    assert code_of(valid.replace(b"%end ", b"%stop ")) == "E-SYNTAX"

    assert code_of((EXAMPLES / "invalid" / "hash-mismatch.askill").read_bytes()) == "E-HASH"
    assert code_of((EXAMPLES / "invalid" / "broken-example.askill").read_bytes()) == "E-SYNTAX"


# --- ADR-0004 boundary -----------------------------------------------------------


def test_card_cannot_declare_verification_criteria() -> None:
    sections = ("criteria", "verify", "acceptance", "checks", "expected", "definition-of-done", "dod")
    for name in sections:
        card = seal(swap("\n[origin]\n", f"\n[{name}]\n> файл существует\n\n[origin]\n"))
        assert code_of(card) == "E-CRITERIA", name

    for directive in ("%criteria", "%verify", "%dod", "%expect"):
        card = seal(swap("%version  1\n", f"{directive}  x\n%version  1\n"))
        assert code_of(card) == "E-CRITERIA", directive

    for key in ("success_criteria", "acceptance-criteria", "expected_verdict", "definition_of_done"):
        card = seal(
            swap('  ~ path = "notes/probe.md"\n', f'  ~ path = "notes/probe.md"\n  ~ {key} = "x"\n')
        )
        assert code_of(card) == "E-CRITERIA", key

    claims_key = seal(swap("\n[origin]\n", "\n[claims]\nverification       true\n\n[origin]\n"))
    assert code_of(claims_key) == "E-CRITERIA"

    for name in ("criteria-section.askill", "criteria-key.askill"):
        assert code_of((EXAMPLES / "invalid" / name).read_bytes()) == "E-CRITERIA"


# --- shipped examples ------------------------------------------------------------


def test_every_valid_example_parses_and_every_invalid_one_is_refused() -> None:
    valid = sorted(EXAMPLES.glob("*.askill"))
    invalid = sorted((EXAMPLES / "invalid").glob("*.askill"))

    assert len(valid) >= 2
    assert len(invalid) >= 2
    for path in valid:
        assert parse_card(path.read_bytes()).format_version == 1, path.name
    for path in invalid:
        with pytest.raises(SkillFormatError):
            parse_card(path.read_bytes())


def test_every_error_code_has_a_worked_example() -> None:
    codes = set()
    for path in sorted((EXAMPLES / "invalid").glob("*.askill")):
        with pytest.raises(SkillFormatError) as excinfo:
            parse_card(path.read_bytes())
        codes.add(excinfo.value.code)

    assert codes == {
        "E-ENC",
        "E-LIMIT",
        "E-ORDER",
        "E-UNKNOWN",
        "E-SLOT",
        "E-HASH",
        "E-SYNTAX",
        "E-VERSION",
        "E-CRITERIA",
    }


def test_errors_carry_a_code_and_a_line_number() -> None:
    with pytest.raises(SkillFormatError) as excinfo:
        parse_card(seal(swap("%version  1\n", "%hint     подсказка\n%version  1\n")))

    error = excinfo.value
    assert error.code == "E-UNKNOWN"
    assert error.line == 4
    assert str(error).startswith("E-UNKNOWN line 4: ")


def test_format_package_still_exports_the_registry_facade() -> None:
    assert SkillState.DRAFT.value == "DRAFT"
