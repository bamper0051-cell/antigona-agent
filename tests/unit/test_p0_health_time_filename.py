"""P0 regression tests: /health structure, system.time routing, file-create path.

D1 — `/health` must answer with a structured report (gateway / worker /
     verifier / time), not a single hardcoded "Gateway доступен" string.
D2 — a Russian date/time request must route to the deterministic `system.time`
     branch and report the real system clock, not a file-write task.
D4 — the filename named right after «создай» wins as target_path even when the
     content is a JSON literal containing keys that look like paths.
"""

from __future__ import annotations

import datetime

from antigona.channels.telegram.bot import build_health_report, collect_health_report
from antigona.router.intent_router import IntentRouter
from antigona.task_goal import parse_goal
from antigona.tools.system_time import format_system_time_reply

# ── D1: structured /health ────────────────────────────────────────────────


def test_health_report_is_structured_not_a_single_string() -> None:
    report = build_health_report(
        gateway="ok",
        services={
            "worker": {"up": True, "last_seen": 1.0, "pid": 1},
            "verifier": {"up": False, "last_seen": 1.0, "pid": 2},
        },
        now_utc="2026-08-23T10:00:00+00:00",
    )
    assert "gateway" in report
    assert "worker" in report
    assert "verifier" in report
    assert "2026-08-23T10:00:00+00:00" in report
    assert report != "✅ Gateway доступен и отвечает."
    assert len(report.splitlines()) >= 4


def test_health_report_reports_missing_heartbeat_honestly() -> None:
    report = build_health_report(gateway="down", services={}, now_utc="t")
    assert "gateway — down" in report
    assert "неизвестно" in report


def test_collect_health_report_uses_real_clock() -> None:
    report = collect_health_report("ok")
    year = str(datetime.datetime.now(datetime.UTC).year)
    assert year in report
    assert "gateway" in report and "verifier" in report


# ── D2: date/time → system.time ───────────────────────────────────────────


def test_date_time_request_routes_to_system_time() -> None:
    router = IntentRouter()
    decision = router.route(
        text="Напиши сегодняшнюю дату и время с сервера",
        context={"source": "cli"},
    )
    assert decision.intent == "question.system_time"
    assert decision.entities["tool"] == "system.time"
    assert not decision.requires_planner
    assert not decision.requires_approval


def test_date_time_request_answer_contains_real_current_year() -> None:
    router = IntentRouter()
    decision = router.route(
        text="Напиши сегодняшнюю дату и время с сервера",
        context={"source": "cli"},
    )
    year = str(datetime.datetime.now(datetime.UTC).year)
    assert year in str(decision.entities["answer"])
    assert year in str(decision.entities["time"]["utc_iso"])


def test_date_time_variants_all_route_to_system_time() -> None:
    router = IntentRouter()
    for text in (
        "Какая сегодня дата?",
        "который час",
        "Покажи текущее время",
        "what time is it",
        "current date please",
    ):
        decision = router.route(text=text, context={"source": "cli"})
        assert decision.intent == "question.system_time", text
        assert not decision.intent.startswith("task."), text


def test_file_write_with_date_phrase_is_not_captured_by_system_time() -> None:
    """D2 over-capture: an explicit file-write wins over the date/time question."""
    router = IntentRouter()
    for text in (
        "Создай файл notes.txt с сегодняшней датой",
        "запиши в файл date.txt текущую дату",
        "Создай report.txt содержащий текущую дату и время",
    ):
        decision = router.route(text=text, context={"source": "cli"})
        assert decision.intent != "question.system_time", text
        assert decision.intent == "task.file_write", text


def test_pure_date_time_questions_still_route_to_system_time() -> None:
    """The guard must not regress the pure question form."""
    router = IntentRouter()
    for text in (
        "Напиши сегодняшнюю дату и время с сервера",
        "какая сегодня дата",
        "который час",
        "current time",
    ):
        decision = router.route(text=text, context={"source": "cli"})
        assert decision.intent == "question.system_time", text


def test_format_system_time_reply_has_real_year() -> None:
    reply = format_system_time_reply()
    assert str(datetime.datetime.now(datetime.UTC).year) in reply


# ── D4: file-create keeps the explicit filename ───────────────────────────


def test_create_json_file_keeps_filename_and_exact_content() -> None:
    plan = parse_goal('Создай foo.json ровно {"system":"x","ok":true}')
    assert plan.path == "foo.json"
    assert plan.content == '{"system":"x","ok":true}'
    assert plan.intent == "file_write"


def test_create_json_file_does_not_take_path_from_json_key() -> None:
    goal = (
        'Создай exam_EXAM23_9f3.json ровно '
        '{"system":"antigona","nonce":"9f3","ok":true}'
    )
    plan = parse_goal(goal)
    assert plan.path == "exam_EXAM23_9f3.json"
    assert plan.content == '{"system":"antigona","nonce":"9f3","ok":true}'
    assert "exam_EXAM23_9f3.json" in plan.expected_paths
