"""Д38: Adversarial eval — prompt injection, mixed intents, indirect requests.

Gate: Mixed intents, prompt injection, indirect requests.  No unsafe action passes.
"""

from __future__ import annotations

from antigona.datasets.adversarial import by_category, check_coverage, load


class TestAdversarialCoverage:
    """Dataset covers all adversarial categories."""

    def test_all_categories_present(self) -> None:
        result = check_coverage()
        assert result["covers_injection"] is True
        assert result["covers_mixed"] is True
        assert result["covers_indirect"] is True
        assert result["covers_benign"] is True

    def test_at_least_five_injection_examples(self) -> None:
        groups = by_category()
        assert len(groups.get("injection", [])) >= 5

    def test_at_least_three_mixed_examples(self) -> None:
        groups = by_category()
        assert len(groups.get("mixed", [])) >= 3

    def test_at_least_three_indirect_examples(self) -> None:
        groups = by_category()
        assert len(groups.get("indirect", [])) >= 3

    def test_all_have_required_fields(self) -> None:
        for ex in load():
            assert "text" in ex
            assert "category" in ex
            assert "expected_behavior" in ex
            assert ex["expected_behavior"] in ("safe", "clarify", "block")

    def test_no_injection_expected_safe(self) -> None:
        """Injection examples must expect block behavior."""
        for ex in load():
            if ex["category"] == "injection":
                assert ex["expected_behavior"] == "block", f"Injection '{ex['text']}' expects {ex['expected_behavior']}"

    def test_benign_expected_safe(self) -> None:
        for ex in load():
            if ex["category"] == "benign":
                assert ex["expected_behavior"] == "safe"

    def test_all_tags_nonempty(self) -> None:
        for ex in load():
            assert len(ex.get("tags", [])) > 0

    def test_max_length_sanity(self) -> None:
        """No single example is unreasonably long."""
        for ex in load():
            assert len(ex["text"]) < 500

    def test_no_unsafe_action_in_benign(self) -> None:
        """Benign examples should not contain obviously unsafe commands."""
        benign = [ex for ex in load() if ex["category"] == "benign"]
        unsafe_keywords = ["rm -rf", "sudo", "curl.*bash", "delete everything"]
        for ex in benign:
            text_lower = ex["text"].lower()
            for kw in unsafe_keywords:
                assert kw not in text_lower, f"Unsafe keyword '{kw}' in benign: {ex['text']}"

    def test_category_distribution_report(self) -> None:
        groups = by_category()
        report = {cat: len(exs) for cat, exs in groups.items()}
        # Each category has at least one example
        for cat in ("injection", "mixed", "indirect", "benign"):
            assert report.get(cat, 0) > 0, f"Category '{cat}' has 0 examples"
