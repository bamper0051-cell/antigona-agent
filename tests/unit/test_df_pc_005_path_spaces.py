"""DF-PC-005: path with spaces must be captured whole, not truncated to last token."""
from antigona.router.intent_router import (
    _FILE_PATH_RE,
    IntentRouter,
    _detect_entities,
    _normalize_entity_path,
)


def test_normalize_entity_path_preserves_spaces():
    # normalize strips leading workspace/ but keeps internal spaces
    assert _normalize_entity_path("workspace/l15 a b.txt") == "l15 a b.txt"
    assert _normalize_entity_path("l15 a b.txt") == "l15 a b.txt"


def test_detect_entities_path_with_spaces():
    e = _detect_entities("Создай файл workspace/l15 a b.txt с текстом SPACES")
    assert e["path"] == "l15 a b.txt"


def test_detect_entities_path_without_spaces():
    e = _detect_entities("Создай файл workspace/l4_seq.txt с текстом FIRST")
    assert e["path"] == "l4_seq.txt"


def test_router_goal_with_spaces_in_path():
    d = IntentRouter().route("Создай файл workspace/l15 a b.txt с текстом SPACES")
    assert d.entities.get("path") == "l15 a b.txt"


def test_router_goal_without_spaces_in_path():
    d = IntentRouter().route("Создай файл live_l2_v2.txt в workspace с текстом OK")
    assert d.entities.get("path") == "live_l2_v2.txt"


def test_regex_not_greedy_over_russian_text():
    # must not swallow "Создай файл" into the path
    m = _FILE_PATH_RE.search("Создай файл workspace/l15 a b.txt с текстом SPACES")
    assert m is not None
    assert m.group(1) == "workspace/l15 a b.txt"
