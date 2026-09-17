"""Tests for ActivityTracker and the persistent status bar."""

from __future__ import annotations

from antigona.cli_ui.activity import ActivityTracker, get_tracker
from antigona.cli_ui.status_bar import (
    _elapsed_label,
    _truncate,
    build_status_bar,
)


def test_tracker_start_list_finish() -> None:
    tracker = ActivityTracker()
    act_id = tracker.start("process", "run tests", detail="pytest -q")
    assert tracker.count(live_only=True) == 1
    live = tracker.list(live_only=True)
    assert live[0].label == "run tests"
    assert live[0].detail == "pytest -q"
    assert live[0].kind == "process"

    tracker.finish(act_id)
    assert tracker.count(live_only=True) == 0
    # Finished activity is still retained in the full view (audit).
    assert tracker.count(live_only=False) == 1
    assert tracker.list(live_only=False)[0].status == "done"


def test_tracker_update() -> None:
    tracker = ActivityTracker()
    act_id = tracker.start("subagent", "web search")
    tracker.update(act_id, detail="3 queries")
    assert tracker.list()[0].detail == "3 queries"
    tracker.update(act_id, status="done")
    assert tracker.list()[0].status == "done"


def test_tracker_clear_and_singleton() -> None:
    tracker = get_tracker()
    tracker.clear()
    tracker.start("process", "x")
    assert get_tracker() is tracker  # same singleton
    assert tracker.count() == 1
    tracker.clear()


def test_elapsed_label_formatting() -> None:
    assert _elapsed_label(5) == "5s"
    assert _elapsed_label(65) == "1m 05s"
    assert _elapsed_label(3665) == "1h 01m"


def test_truncate() -> None:
    assert _truncate("hello world", 5) == "hell…"
    assert _truncate("hello world", 50) == "hello world"


def test_status_bar_idle() -> None:
    tracker = get_tracker()
    tracker.clear()
    html = build_status_bar()
    assert "idle" in html.value


def test_status_bar_active() -> None:
    tracker = get_tracker()
    tracker.clear()
    tracker.start("subagent", "web search", detail="3 queries")
    tracker.start("process", "code gen")
    html = build_status_bar()
    assert "web search" in html.value
    assert "code gen" in html.value
    tracker.clear()


def test_status_bar_overflow() -> None:
    tracker = get_tracker()
    tracker.clear()
    for i in range(5):
        tracker.start("process", f"extra task {i}")
    html = build_status_bar()
    # 3 inline + (5-3)=2 hidden.
    assert "+2 more" in html.value
    tracker.clear()
