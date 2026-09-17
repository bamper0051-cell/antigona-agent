"""Looper-inspired agent loop runner.

A small, dependency-free implementation of the goal -> plan -> review -> judge
-> stop loop idea (from the `looper` skill). Executes a plan of steps, runs a
reviewer/judge pass, and stops cleanly on success or a max-iteration guard.

This is a *framework primitive*: the actual step/review/judge callables are
injected, so it composes with Antigona's worker/orchestrator and any LLM.

Usage::

    from antigona.core.loop import run_loop

    steps = [lambda ctx: {...}, ...]
    outcome = run_loop(steps=steps, judge=lambda result: ("PASS", "") )
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["run_loop", "LoopOutcome", "MAX_ITERATIONS_DEFAULT"]

MAX_ITERATIONS_DEFAULT = 10


@dataclass
class LoopOutcome:
    status: str  # "delivered" | "stopped" | "failed"
    iterations: int
    final_result: Any = None
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def run_loop(
    *,
    steps: list[Callable[[dict[str, Any]], Any]],
    judge: Callable[[Any], tuple[str, str]] | None = None,
    context: dict[str, Any] | None = None,
    max_iterations: int = MAX_ITERATIONS_DEFAULT,
    on_step_error: str = "stop",  # "stop" | "continue"
) -> LoopOutcome:
    """Execute a step list through a judge gate, with a termination guard.

    - ``steps``: ordered callables; each receives ``context`` and returns a
      result (a dict, a value, a file path, ...).
    - ``judge(result) -> (verdict, notes)``: ``verdict`` in ``PASS``/``FAIL``.
      If no judge is given, the loop runs all steps once and returns
      ``delivered``.
    - Stops cleanly on first PASS (``delivered``), on max_iterations, or on a
      step exception (``failed``) depending on ``on_step_error``.
    """
    ctx = dict(context or {})
    notes: list[str] = []
    iterations = 0

    for i in range(max_iterations):
        iterations += 1
        try:
            result = None
            for step in steps:
                result = step(ctx)
        except Exception as exc:
            notes.append(f"step error (iter {i + 1}): {exc}")
            if on_step_error == "stop":
                return LoopOutcome("failed", iterations, None, "", notes)
            continue  # continue despite error

        if judge is None:
            return LoopOutcome("delivered", iterations, result, "PASS (no judge)", notes)

        try:
            verdict, note = judge(result)
        except Exception as exc:
            notes.append(f"judge error (iter {i + 1}): {exc}")
            return LoopOutcome("failed", iterations, result, "", notes)

        notes.append(f"iter {i + 1}: {verdict} {note}")
        if verdict.upper() == "PASS":
            return LoopOutcome("delivered", iterations, result, verdict, notes)

    return LoopOutcome("stopped", iterations, None, "max_iterations", notes)
