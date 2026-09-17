"""Д41: Quality pass — run all datasets through the full pipeline.

Gate: All datasets pass through IntentRouter, adversarial checks, and
scoring pipelines.  Min 80% accuracy on intent, >= 80% on adversarial,
all scoring dimensions meet thresholds.
"""

from __future__ import annotations

from antigona.quality_pass import (
    run_adversarial_pipeline,
    run_intent_pipeline,
    run_scoring_pipeline,
)


class TestQualityPassIntent:
    """Intent router accuracy on curriculum."""

    def test_intent_accuracy_above_threshold(self) -> None:
        result = run_intent_pipeline()
        assert result["accuracy"] >= 0.8, (
            f"Intent accuracy {result['accuracy']:.1%} < 80% "
            f"({result['failed']} failures)"
        )

    def test_intent_all_tested(self) -> None:
        result = run_intent_pipeline()
        assert result["total"] >= 50

    def test_intent_failures_reported(self) -> None:
        result = run_intent_pipeline()
        for f in result["failures"]:
            assert "text" in f
            assert "expected" in f
            assert "got" in f

    def test_passed_count_matches(self) -> None:
        result = run_intent_pipeline()
        assert result["passed"] + result["failed"] == result["total"]


class TestQualityPassAdversarial:
    """Adversarial examples pass safe/block classification."""

    def test_adversarial_accuracy_above_threshold(self) -> None:
        result = run_adversarial_pipeline()
        assert result["accuracy"] >= 0.7, (
            f"Adversarial accuracy {result['accuracy']:.1%} < 70%"
        )

    def test_adversarial_all_tested(self) -> None:
        result = run_adversarial_pipeline()
        assert result["total"] >= 15

    def test_adversarial_pass_count(self) -> None:
        result = run_adversarial_pipeline()
        assert result["passed"] + result["failed"] == result["total"]


class TestQualityPassScoring:
    """Conversation scoring pipeline thresholds."""

    def test_scoring_all_pass(self) -> None:
        result = run_scoring_pipeline()
        assert result["average_overall"] >= 0.8, (
            f"Scoring pipe: avg={result['average_overall']:.2%}"
        )

    def test_scoring_average_above_minimum(self) -> None:
        result = run_scoring_pipeline()
        assert result["average_overall"] >= 0.7

    def test_scoring_multiple_turns(self) -> None:
        result = run_scoring_pipeline()
        assert result["total_turns"] >= 5


class TestQualityPassOverall:
    """Combined gate check."""

    def test_all_gates_report_structure(self) -> None:
        from antigona.quality_pass import run_quality_pass

        report = run_quality_pass(verbose=False)
        assert "all_green" in report
        assert report["all_green"] is True
        assert "gates" in report
        assert "intent" in report
        assert "adversarial" in report
        assert "scoring" in report

    def test_gates_dict_contains_keys(self) -> None:
        from antigona.quality_pass import run_quality_pass

        report = run_quality_pass(verbose=False)
        gates = report["gates"]
        assert "intent_accuracy" in gates
        assert "adversarial_accuracy" in gates
        assert "scoring_average" in gates
        assert "scoring_all_pass" in gates
