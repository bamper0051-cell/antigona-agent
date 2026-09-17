"""P0-R1 (2026-08-27): _CONTENT_TWO_LINES_RE must match LIVE gateway wording.

HEAD only matched "содержимое (должно быть) ровно (две строки|двумя строками):".
The LIVE draft literal uses "с ТОЧНЫМ содержимым из двух строк:" — a different
declension (содержимым, instrumental) and word order (из двух строк) — which did
NOT match, producing the gateway error "draft missing request literals".

Regression guard: the fix must NOT reproduce the archived dirty diff's DOTALL
'.+' overcapture (evidence/loop_engineer_p0r1_20260827T082754Z_c5e4b48a/
02_hunk_isolate/task_goal_P0R1_uncommitted.diff), which swallowed trailing
instruction prose into the receipt content.
"""
from antigona.task_goal import _CONTENT_TWO_LINES_RE, _extract_content, parse_goal

P0R1_LIVE_GOAL = (
    "Создай файл workspace/receipt.txt с ТОЧНЫМ содержимым из двух строк:\n"
    "RECEIPT TEST\n"
    "77392521\n"
    "После записи обязательно прочитай файл обратно и покажи путь, "
    "инструмент записи, инструмент чтения и точное содержимое."
)


def test_old_phrase_dolzhno_byt_still_matches():
    """1) Regression: original 'должно быть ровно две строки:' wording."""
    goal = "Содержимое должно быть ровно две строки: RECEIPT TEST\n77392521"
    assert _CONTENT_TWO_LINES_RE.search(goal)
    assert _extract_content(goal) == "RECEIPT TEST\n77392521"


def test_p0r1_exact_live_wording_matches():
    """2) The exact LIVE P0-R1 phrase must now match and extract clean content."""
    assert _CONTENT_TWO_LINES_RE.search(P0R1_LIVE_GOAL)
    assert _extract_content(P0R1_LIVE_GOAL) == "RECEIPT TEST\n77392521"


def test_dvumya_strokami_variant_matches():
    """3) 'двумя строками' declension, without 'ровно'/'из'."""
    goal = "с точным содержимым двумя строками: RECEIPT TEST\n77392521"
    assert _CONTENT_TWO_LINES_RE.search(goal)
    assert _extract_content(goal) == "RECEIPT TEST\n77392521"


def test_negative_one_line_does_not_match_two_line_pattern():
    """4a) 'одной строки' must NOT be treated as the two-line pattern."""
    goal = "с точным содержимым из одной строки: RECEIPT TEST"
    assert _CONTENT_TWO_LINES_RE.search(goal) is None


def test_negative_empty_after_colon_yields_no_receipt():
    """4b) Nothing after the colon must not fabricate receipt content."""
    goal = "Создай файл workspace/x.txt с точным содержимым из двух строк:   "
    content = _extract_content(goal)
    assert "RECEIPT" not in content
    assert "77392521" not in content


def test_negative_trailing_prose_not_captured_as_content():
    """4c) Trailing instruction sentence after the two literal lines must be
    excluded from content."""
    content = _extract_content(P0R1_LIVE_GOAL)
    assert content == "RECEIPT TEST\n77392521"
    assert "прочитай" not in content
    assert "После записи" not in content


def test_p0r1_path_extraction_unaffected():
    """5) Path extraction on the exact P0-R1 wording remains correct."""
    plan = parse_goal(P0R1_LIVE_GOAL)
    assert plan.intent == "file_write_read", plan
    assert plan.path == "receipt.txt", plan.path
    assert plan.content == "RECEIPT TEST\n77392521", repr(plan.content)
    assert plan.read_after_write is True
