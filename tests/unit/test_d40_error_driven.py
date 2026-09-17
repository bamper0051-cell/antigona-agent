"""Д40: Error-driven learning — user corrections → regression cases.

Gate: Every user correction is captured as a regression case.  Dataset covers
intent misclassifications, clarification failures, scope errors, safety overrides.
"""

from __future__ import annotations

from antigona.datasets.corrections import by_tag, correction_examples, load


class TestErrorDrivenLearning:
    """Corrections dataset is comprehensive and structured."""

    def test_has_examples(self) -> None:
        assert len(correction_examples) >= 10

    def test_all_have_required_fields(self) -> None:
        for ex in correction_examples:
            assert "user_text" in ex, f"Missing user_text in {ex}"
            assert "correction" in ex, f"Missing correction in {ex}"
            assert "expected_intent" in ex, f"Missing expected_intent in {ex}"

    def test_correction_different_from_original(self) -> None:
        """Correction should be different from the original user text."""
        for ex in correction_examples:
            assert ex["correction"] != ex["user_text"], (
                f"Correction same as user_text: {ex['user_text']}"
            )

    def test_coverage_intent_types(self) -> None:
        """Should cover multiple intent types."""
        intents = {ex["expected_intent"] for ex in correction_examples}
        assert "task.file_write" in intents or "task.shell" in intents
        assert len(intents) >= 3

    def test_intent_misclassification_present(self) -> None:
        intent_corrections = by_tag("intent")
        assert len(intent_corrections) >= 3

    def test_clarification_corrections_present(self) -> None:
        clarifications = by_tag("clarification")
        assert len(clarifications) >= 2

    def test_user_text_and_correction_both_populated(self) -> None:
        for ex in correction_examples:
            assert len(ex["user_text"].strip()) > 0
            assert len(ex["correction"].strip()) > 0

    def test_original_response_present(self) -> None:
        for ex in correction_examples:
            assert "original_response" in ex
            assert len(ex["original_response"]) > 0

    def test_tags_populated(self) -> None:
        for ex in correction_examples:
            assert len(ex.get("tags", [])) > 0

    def test_followup_corrections(self) -> None:
        followups = by_tag("followup")
        assert len(followups) >= 2

    def test_safety_corrections(self) -> None:
        safety = by_tag("safety")
        assert len(safety) >= 1

    def test_load_function(self) -> None:
        loaded = load()
        assert len(loaded) == len(correction_examples)
