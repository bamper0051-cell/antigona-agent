"""Clean-room negative probes for the P3.1 subagents implementation.

Two invariants from P3_1_PLAN.md §5.1 are asserted straight against the source
text, because provenance is invisible to behavioural tests:

* the subagents code (``orchestrator.py``, ``worker/__init__.py``) borrows nothing
  from upstream agent shells (Hermes, OpenClaw, OpenHands, klio-tech);
* ``spawn_child_flow`` / ``aggregate_child_results`` never finalize a flow — only
  the Verifier owns the terminal ``DONE`` transition (consistent with ADR-0004).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "antigona"

FORBIDDEN_MARKERS = ["klio", "openhands", "hermes", "openclaw"]


def _source(relpath: str) -> str:
    return (SRC / relpath).read_text(encoding="utf-8")


def test_orchestrator_has_no_upstream_markers() -> None:
    lowered = _source("orchestrator.py").lower()
    for banned in FORBIDDEN_MARKERS:
        assert banned not in lowered, f"orchestrator.py references forbidden source: {banned}"


def test_worker_has_no_upstream_markers() -> None:
    lowered = _source("worker/__init__.py").lower()
    for banned in FORBIDDEN_MARKERS:
        assert banned not in lowered, f"worker/__init__.py references forbidden source: {banned}"


def _function_body(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


def test_spawn_and_aggregate_do_not_finalize() -> None:
    """Neither spawn nor aggregate may drive a flow to the Verifier-owned DONE.

    Reading a child's status (comparing against ``TaskState.DONE.value``) is fine;
    what is forbidden is *transitioning* a flow to that terminal state.
    """
    source = _source("orchestrator.py")
    for fn in ("spawn_child_flow", "aggregate_child_results"):
        occurrences = [ast.get_source_segment(source, n) or ""
                       for n in ast.walk(ast.parse(source))
                       if isinstance(n, ast.FunctionDef) and n.name == fn]
        assert occurrences, f"function {fn} not found"
        for body in occurrences:
            assert "repository.transition(" not in body, (
                f"{fn} must not run a task state-machine transition"
            )
            assert "transition_step(" not in body, (
                f"{fn} must not run a step state-machine transition"
            )


def test_aggregate_is_read_only() -> None:
    """aggregate_child_results performs no writes (SELECT-only)."""
    source = _source("orchestrator.py")
    body = ""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "aggregate_child_results":
            seg = ast.get_source_segment(source, node) or ""
            if "def aggregate_child_results(self" in seg:
                body = seg
    assert body, "Orchestrator.aggregate_child_results not found"
    for write in ("session.add", ".commit(", ".flush(", "transition("):
        assert write not in body, f"aggregate_child_results must be read-only, found: {write}"
