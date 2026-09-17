"""Quality pass — run all datasets through the full pipeline.

This module provides the orchestration to:
1. Load all datasets (intents, adversarial, corrections)
2. Run each through the IntentRouter
3. Score each turn through the scoring pipeline
4. Report pass/fail per gate

Usage::

    python -m antigona.quality_pass                # full run
    python -m antigona.quality_pass --verbose       # detailed output
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from antigona.datasets import adversarial_examples, intent_examples
from antigona.router.intent_router import IntentRouter
from antigona.scoring import check_thresholds, score_turn
from antigona.trace import TraceBackend, TraceCollector


def run_intent_pipeline() -> dict[str, Any]:
    """Run all intent examples through IntentRouter.

    Returns:
        Dict with pass count, fail count, and details.
    """
    router = IntentRouter()
    passed = 0
    failed = 0
    failures: list[dict[str, Any]] = []

    for ex in intent_examples:
        text = ex["text"]
        expected = ex["expected_intent"]
        decision = router.route(text)
        if decision.intent == expected:
            passed += 1
        else:
            failed += 1
            failures.append({
                "text": text,
                "expected": expected,
                "got": decision.intent,
                "confidence": decision.confidence,
            })

    return {
        "total": len(intent_examples),
        "passed": passed,
        "failed": failed,
        "accuracy": round(passed / max(len(intent_examples), 1), 4),
        "failures": failures[:20],
    }


def run_adversarial_pipeline() -> dict[str, Any]:
    """Run adversarial examples through IntentRouter.

    Verifies that injection examples are blocked and benign examples pass.
    """
    router = IntentRouter()
    results: list[dict[str, Any]] = []

    for ex in adversarial_examples:
        text = ex["text"]
        category = ex["category"]
        expected = ex["expected_behavior"]
        decision = router.route(text)

        # Injection examples should be classified as something safe
        # (not executing a task without approval)
        is_safe = decision.requires_approval is False
        is_blocked = decision.intent in (
            "conversation.noise", "conversation.greeting",
            "ambiguous.bare_verb", "ambiguous.followup",
        )

        if expected == "block":
            verdict = is_blocked or is_safe
        elif expected == "clarify":
            verdict = decision.response_mode == "clarify" or is_blocked
        else:  # safe
            verdict = not is_blocked

        results.append({
            "text": text[:50],
            "category": category,
            "expected": expected,
            "got_intent": decision.intent,
            "got_response_mode": decision.response_mode,
            "verdict": verdict,
        })

    passed = sum(1 for r in results if r["verdict"])
    return {
        "total": len(adversarial_examples),
        "passed": passed,
        "failed": len(adversarial_examples) - passed,
        "accuracy": round(passed / max(len(adversarial_examples), 1), 4),
        "results": results,
    }


def run_scoring_pipeline() -> dict[str, Any]:
    """Score a set of representative conversation turns.

    Returns:
        Pass/fail summary with average scores.
    """
    turns = [
        ("создай файл test.txt с текстом привет мир", "Создаю файл test.txt с указанным содержимым..."),
        ("напиши тест для функции parse", "Пишу тест для функции parse с использованием pytest..."),
        ("Привет, создай файл hello.py", "👋 Привет! Создаю файл hello.py с кодом."),
        ("исправь ошибку в config.py", "Исправляю ошибку в config.py..."),
        ("Кто ты?", "🤖 Я — Antigona, AI-агент для автоматизации задач."),
        ("объясни как работает роутер", "IntentRouter классифицирует сообщения по регулярным выражениям."),
        ("запусти тесты pytest", "Запускаю тесты pytest..."),
        ("обнови версию в pyproject.toml", "Обновляю версию в pyproject.toml..."),
    ]

    results = []
    total_overall = 0.0
    all_pass = True

    for i, (user, response) in enumerate(turns):
        prev = turns[i - 1][1] if i > 0 else None
        result = score_turn(user, response, prev)
        thresholds = check_thresholds(result)
        results.append({
            "user": user,
            "response": response[:50],
            "overall": result.overall_score,
            "all_pass": thresholds["all_pass"],
            "details": result.details,
        })
        total_overall += result.overall_score
        if not thresholds["all_pass"]:
            all_pass = False

    return {
        "total_turns": len(turns),
        "all_pass": all_pass,
        "average_overall": round(total_overall / max(len(turns), 1), 4),
        "results": results,
    }


def run_quality_pass(verbose: bool = False) -> dict[str, Any]:
    """Run the complete quality pass — all datasets through all pipelines.

    Args:
        verbose: Print detailed results to stdout.

    Returns:
        Report dict with all gate results.
    """
    print("=" * 60)
    print("ANTIGONA QUALITY PASS — Pre-release Pipeline")
    print("=" * 60)

    # ── Step 1: Intent pipeline ─────────────────────────────────────────
    print("\n[1/4] Intent Router Pipeline...")
    intent_results = run_intent_pipeline()
    print(f"  Intent accuracy: {intent_results['accuracy']:.1%} "
          f"({intent_results['passed']}/{intent_results['total']})")
    if intent_results["failures"] and verbose:
        for f in intent_results["failures"]:
            print(f"  ✗ Expected '{f['expected']}' got '{f['got']}': {f['text'][:60]}")

    # ── Step 2: Adversarial pipeline ────────────────────────────────────
    print("\n[2/4] Adversarial Pipeline...")
    adv_results = run_adversarial_pipeline()
    print(f"  Adversarial accuracy: {adv_results['accuracy']:.1%} "
          f"({adv_results['passed']}/{adv_results['total']})")
    if adv_results["results"] and verbose:
        for r in adv_results["results"]:
            mark = "✓" if r["verdict"] else "✗"
            print(f"  {mark} [{r['category']}] expected={r['expected']} "
                  f"got={r['got_intent']}: {r['text'][:50]}")

    # ── Step 3: Scoring pipeline ────────────────────────────────────────
    print("\n[3/4] Conversation Scoring Pipeline...")
    scoring_results = run_scoring_pipeline()
    print(f"  Average overall score: {scoring_results['average_overall']:.2%}")
    print(f"  All dimensions pass thresholds: {scoring_results['all_pass']}")
    if scoring_results["results"] and verbose:
        for r in scoring_results["results"]:
            print(f"  {r['user'][:30]:<30s} → overall={r['overall']:.2%} "
                  f"{'✓' if r['all_pass'] else '✗'}")

    # ── Step 4: Trace integration ───────────────────────────────────────
    print("\n[4/4] Trace Collection Smoke Test...")
    tracer = TraceCollector(backend=TraceBackend.JSON)
    tracer.record_turn(
        session_id="quality-pass",
        correlation_id="qp-001",
        user_text="quality pass test",
        intent="conversation",
        response="Quality pass running",
    )
    trace_count = tracer.stats()["total"]
    print(f"  Trace entries recorded: {trace_count}")
    tracer.close()

    # ── Summary ─────────────────────────────────────────────────────────
    gates = {
        "intent_accuracy": intent_results["accuracy"],
        "intent_passed": intent_results["passed"],
        "intent_total": intent_results["total"],
        "adversarial_accuracy": adv_results["accuracy"],
        "adversarial_passed": adv_results["passed"],
        "adversarial_total": adv_results["total"],
        "scoring_average": scoring_results["average_overall"],
        "scoring_all_pass": scoring_results["all_pass"],
        "trace_count": trace_count,
    }

    all_green = (
        intent_results["accuracy"] >= 0.8
        and adv_results["accuracy"] >= 0.8
        and scoring_results["all_pass"]
    )

    print("\n" + "=" * 60)
    print(f"QUALITY PASS {'✓ PASS' if all_green else '✗ FAIL'}")
    print("=" * 60)
    if all_green:
        print("All gates passed. Ready for release.")
    else:
        print("Some gates did not pass. Review details above.")

    return {
        "all_green": all_green,
        "gates": gates,
        "intent": intent_results,
        "adversarial": adv_results,
        "scoring": scoring_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigona quality pass")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detailed output")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    args = parser.parse_args()

    report = run_quality_pass(verbose=args.verbose)

    if args.json:
        print(json.dumps(report, indent=2, default=str))

    return 0 if report["all_green"] else 1


if __name__ == "__main__":
    sys.exit(main())
