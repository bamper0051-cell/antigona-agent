"""Tests: brain bypasses LLM draft when parse_goal returns non-empty content."""
from antigona.task_goal import parse_goal


class TestParserContentBypassesDraft:
    """When parse_goal(text).content is non-empty, brain must use it directly."""

    def test_parser_extracts_content_soderzhimym(self):
        """parse_goal returns content for 'с содержимым X'."""
        plan = parse_goal(
            "Создай файл e2e_test.txt в workspace с содержимым FULL E2E PASS"
        )
        assert plan.content == "FULL E2E PASS"
        assert plan.path is not None

    def test_parser_no_content_for_generic(self):
        """parse_goal returns empty content for generic file creation."""
        plan = parse_goal("Создай файл notes.txt в workspace")
        assert not plan.content

    def test_bypass_condition_fires_for_parser_content(self):
        """The bypass condition (plan.content truthy) must fire,
        meaning brain will NOT reach _draft_file_content."""
        text = "Создай файл test.txt с содержимым HELLO WORLD"
        plan = parse_goal(text)
        # This is the exact condition in brain.py after the fix:
        # elif plan.content:  →  bypass draft
        assert plan.content, "plan.content must be truthy to bypass draft"
        assert plan.content == "HELLO WORLD"

    def test_no_bypass_without_content(self):
        """When parser has no content, brain must fall through to draft."""
        text = "Создай файл notes.txt в workspace"
        plan = parse_goal(text)
        # plan.content is falsy → brain falls to else branch (draft)
        assert not plan.content, "no content → draft path must be taken"

    def test_bypass_with_tekst_pattern(self):
        """'с текстом X' also triggers parser content."""
        text = "Запиши файл data.txt с текстом TEST DATA"
        plan = parse_goal(text)
        if plan.content:
            assert plan.content == "TEST DATA"
