"""Tests for antigona.core.loop."""

from __future__ import annotations

from antigona.core.loop import run_loop


def test_runs_all_steps_no_judge():
    calls = []

    def s1(ctx):
        calls.append("s1")
        return "r1"

    def s2(ctx):
        calls.append("s2")
        return "r2"

    out = run_loop(steps=[s1, s2])
    assert out.status == "delivered"
    assert calls == ["s1", "s2"]
    assert out.final_result == "r2"


def test_judge_pass_stops_early():
    calls = []

    def step(ctx):
        calls.append(1)
        return "result"

    def judge(result):
        return ("PASS", "looks good")

    out = run_loop(steps=[step], judge=judge, max_iterations=5)
    assert out.status == "delivered"
    assert out.verdict == "PASS"
    assert len(calls) == 1


def test_judge_fail_loops_until_max():
    calls = []

    def step(ctx):
        calls.append(1)
        return "result"

    def judge(result):
        return ("FAIL", "retry")

    out = run_loop(steps=[step], judge=judge, max_iterations=3)
    assert out.status == "stopped"
    assert out.iterations == 3
    assert len(calls) == 3


def test_judge_fail_then_pass():
    results = ["FAIL", "PASS"]
    idx = {"i": 0}

    def step(ctx):
        return "r"

    def judge(result):
        v = results[idx["i"]]
        idx["i"] += 1
        return (v, "")

    out = run_loop(steps=[step], judge=judge, max_iterations=5)
    assert out.status == "delivered"
    assert out.iterations == 2


def test_step_error_stops():
    def bad(ctx):
        raise RuntimeError("boom")

    out = run_loop(steps=[bad])
    assert out.status == "failed"
    assert "boom" in out.notes[0]


def test_max_iterations_default():
    def step(ctx):
        return "x"

    def judge(result):
        return ("FAIL", "")

    out = run_loop(steps=[step], judge=judge)
    assert out.status == "stopped"
    assert out.iterations == 10  # default
