"""Unit test for defect DF-PC-004: multi-file goal handling in IntentRouter.

Defect:
Goal «Запиши в файл workspace/l3_a.txt слово ALPHA. И отдельно запиши в файл workspace/l3_b.txt слово BETA»
previously extracted entities={'path': 'l3_a.txt'} and routed as task.file_write for l3_a.txt only.
This caused the second file l3_b.txt to be dropped and content to be merged/lost.

Fix:
When multiple files are detected (two or more _FILE_PATH_RE occurrences in goal text),
_detect_entities returns both 'path' (first path) and 'paths' (list of all normalized paths).
IntentRouter.route does NOT classify as task.file_write for a single file, but instead returns
response_mode="clarify", reason_code="multi_file_requires_spec" (or ambiguous.mixed_intent),
preventing data loss and false single-file DONE execution.
"""

from __future__ import annotations

from antigona.router.intent_router import IntentRouter, _detect_entities


def test_detect_entities_multiple_files():
    text = "Запиши в файл workspace/l3_a.txt слово ALPHA. И отдельно запиши в файл workspace/l3_b.txt слово BETA"
    entities = _detect_entities(text)
    assert entities.get("path") == "l3_a.txt"
    assert entities.get("paths") == ["l3_a.txt", "l3_b.txt"]


def test_router_multi_file_not_single_file_write():
    router = IntentRouter()
    goal1 = "Запиши в файл workspace/l3_a.txt слово ALPHA. И отдельно запиши в файл workspace/l3_b.txt слово BETA"
    decision1 = router.route(goal1)

    # Must NOT route to task.file_write for single file
    assert decision1.intent != "task.file_write"
    assert decision1.response_mode == "clarify"
    assert decision1.reason_code == "multi_file_requires_spec"
    assert decision1.entities.get("paths") == ["l3_a.txt", "l3_b.txt"]


def test_router_multi_file_variant_and_separately():
    router = IntentRouter()
    goal2 = "Создай файл a.txt со словом ONE и файл b.txt со словом TWO"
    decision2 = router.route(goal2)

    assert decision2.intent != "task.file_write"
    assert decision2.response_mode == "clarify"
    assert decision2.reason_code == "multi_file_requires_spec"
    assert decision2.entities.get("paths") == ["a.txt", "b.txt"]


def test_router_single_file_write_preserved():
    router = IntentRouter()
    decision = router.route("Создай файл hello.txt с текстом HELLO")

    assert decision.intent == "task.file_write"
    assert decision.response_mode == "task_preview"
    assert decision.entities.get("path") == "hello.txt"
    assert decision.entities.get("paths") is None  # single file -> no multi 'paths' key


def test_router_single_file_workspace_prefix_preserved():
    router = IntentRouter()
    decision = router.route("Создай файл workspace/l4_seq.txt с текстом FIRST")

    assert decision.intent == "task.file_write"
    assert decision.response_mode == "task_preview"
    assert decision.entities.get("path") == "l4_seq.txt"
    assert decision.entities.get("paths") is None


def test_router_spaces_in_path_preserved():
    router = IntentRouter()
    decision = router.route("Создай файл workspace/l15 a b.txt с текстом SPACES")

    assert decision.intent == "task.file_write"
    assert decision.response_mode == "task_preview"
    assert decision.entities.get("path") == "l15 a b.txt"
    assert decision.entities.get("paths") is None
