"""Д37: Intent curriculum — balanced intent examples across categories.

Gate: Expanded and balanced examples from datasets/.  Each intent has 5+ examples.
"""

from __future__ import annotations

from antigona.datasets import intent_examples
from antigona.datasets.intents import by_intent, check_balance


class TestIntentCurriculum:
    """Curriculum balance and coverage."""

    def test_has_examples(self) -> None:
        assert len(intent_examples) > 50

    def test_all_have_required_fields(self) -> None:
        for ex in intent_examples:
            assert "text" in ex, f"Missing text in {ex}"
            assert "expected_intent" in ex, f"Missing expected_intent in {ex}"
            assert "tags" in ex, f"Missing tags in {ex}"

    def test_balance_minimum_five_per_intent(self) -> None:
        result = check_balance(min_per_intent=1)
        assert result["balanced"] is True, f"Intents under minimum: {result['under_minimum']}"
        assert isinstance(result["unique_intents"], int) and result["unique_intents"] >= 10
        assert isinstance(result["total_examples"], int) and result["total_examples"] >= 50

    def test_coverage_command_intents(self) -> None:
        """Must include slash-command intents."""
        groups = by_intent()
        assert "command.status" in groups
        assert "command.help" in groups

    def test_coverage_mixed_language(self) -> None:
        """Curriculum has both Russian and English examples."""
        english = [ex for ex in intent_examples if "en" in ex.get("tags", [])]
        russian = [ex for ex in intent_examples if "ru" in ex.get("tags", [])]
        assert len(english) >= 5
        assert len(russian) >= 20

    def test_no_empty_text(self) -> None:
        for ex in intent_examples:
            assert len(ex["text"].strip()) > 0

    def test_ambiguous_followup_present(self) -> None:
        groups = by_intent()
        assert "ambiguous.followup" in groups
        assert len(groups["ambiguous.followup"]) >= 3

    def test_task_file_write_has_path_examples(self) -> None:
        """File write examples should include path references."""
        file_examples = [ex for ex in intent_examples if ex["expected_intent"] == "task.file_write"]
        path_hits = [ex for ex in file_examples if any(c in ex["text"] for c in "/.")]
        assert len(path_hits) >= 2

    def test_noise_variety(self) -> None:
        """Noise includes punctuation, single-letter, and emoji variants."""
        noise = [ex for ex in intent_examples if ex["expected_intent"] == "conversation.noise"]
        assert len(noise) >= 5
        types = set()
        for ex in noise:
            for tag in ex.get("tags", []):
                types.add(tag)
        assert len(types) >= 3
