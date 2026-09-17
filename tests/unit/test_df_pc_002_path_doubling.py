"""Unit test for defect DF-PC-002: path doubling prevention.

Bug: goal "Создай файл workspace/l4_seq.txt с текстом FIRST" previously extracted
entities['path'] as 'workspace/l4_seq.txt', causing filesystem write to resolve
full_path as workspace / 'workspace/l4_seq.txt' -> workspace/workspace/l4_seq.txt.

Fix: _detect_entities (and _normalize_entity_path / _normalize_path) strips
leading 'workspace/' or '/workspace/' prefix so that resolving against workspace
results in workspace/l4_seq.txt (single workspace path).
"""

from pathlib import Path

from antigona.filesystem import InProcessTestBackend
from antigona.router.intent_router import IntentRouter, _normalize_entity_path
from antigona.task_goal import parse_goal


def test_intent_router_entity_path_normalization():
    """_detect_entities and _normalize_entity_path strip leading workspace/ prefixes."""
    assert _normalize_entity_path("workspace/l4_seq.txt") == "l4_seq.txt"
    assert _normalize_entity_path("/workspace/nested/file.txt") == "nested/file.txt"
    assert _normalize_entity_path("./local/test.txt") == "local/test.txt"
    assert _normalize_entity_path("live_l2_v2.txt") == "live_l2_v2.txt"


def test_classify_intent_goal_with_workspace_prefix():
    """Goal with 'workspace/<name>' extracts path as '<name>' without doubling."""
    router = IntentRouter()
    decision = router.route("Создай файл workspace/l4_seq.txt с текстом FIRST")
    
    assert decision.intent == "task.file_write"
    assert decision.entities.get("path") == "l4_seq.txt"


def test_classify_intent_goal_without_workspace_prefix():
    """Goal without 'workspace/' prefix (e.g., 'live_l2_v2.txt в workspace') keeps file name intact."""
    router = IntentRouter()
    decision = router.route("Создай файл live_l2_v2.txt в workspace с текстом OK")
    
    assert decision.intent == "task.file_write"
    assert decision.entities.get("path") == "live_l2_v2.txt"


def test_filesystem_write_with_normalized_path(tmp_path: Path):
    """Writing through InProcessTestBackend using router decision path places file in workspace root."""
    router = IntentRouter()
    decision = router.route("Создай файл workspace/l4_seq.txt с текстом FIRST")
    target_path = decision.entities["path"]

    backend = InProcessTestBackend(workspace=tmp_path, test_mode=True)
    res = backend.write(path=target_path, content="FIRST", timeout=5)

    assert res.ok is True
    expected_file = tmp_path / "l4_seq.txt"
    doubled_file = tmp_path / "workspace" / "l4_seq.txt"

    assert expected_file.exists()
    assert expected_file.read_text(encoding="utf-8") == "FIRST"
    assert not doubled_file.exists()


def test_task_goal_parse_goal_workspace_prefix():
    """parse_goal also strips leading workspace/ from target path."""
    plan = parse_goal("Создай файл workspace/l4_seq.txt с текстом FIRST")
    assert plan.path == "l4_seq.txt"
