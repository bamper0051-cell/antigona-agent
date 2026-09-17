"""P1-02 (Antigona R2 Codex review): Parser content-marker interrogative bypass.

Canonical finding — evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md:

    [P1] src/antigona/task_goal.py:68-81,999-1005 —
    «Можешь создать файл a.txt с текстом?» считается content-bearing из-за одного
    маркера «с текстом», поэтому interrogative guard отключается и создаётся
    file_write с «?» как содержимым — вопрос пользователя вызывает
    непреднамеренную запись — negative-тесты не содержат content-clause,
    а content-tests используют только повелительную форму — blocking
"""

from __future__ import annotations

import pytest

from antigona.task_goal import parse_goal


@pytest.mark.parametrize(
    "goal",
    [
        "Можешь создать файл a.txt с текстом?",
        "Можно ли создать файл a.txt с содержимым?",
        "можешь создать файл a.txt с кодом?",
        "Could you create file a.txt with text?",
        "Can you create file a.txt with content?",
        "не мог бы ты создать файл report.txt с текстом?",
        "получится ли создать файл data.json с содержимым?",
    ],
)
def test_interrogative_with_content_marker_is_dialog(goal: str) -> None:
    """A capability question with a bare content marker must NOT open a blind write."""
    plan = parse_goal(goal)
    assert plan.intent == "dialog", f"Goal '{goal}' unexpectedly produced intent='{plan.intent}'"
    assert plan.expected_paths == (), f"Goal '{goal}' unexpectedly set expected_paths={plan.expected_paths}"
    assert plan.note == "interrogative_request", f"Goal '{goal}' note={plan.note}"


@pytest.mark.parametrize(
    ("goal", "expected_path", "expected_content"),
    [
        ("Создай файл a.txt с текстом: привет", "a.txt", "привет"),
        ("Создай файл a.txt с текстом: кто здесь?", "a.txt", "кто здесь?"),
        ('создай файл r.txt с текстом "Готово?"', "r.txt", '"Готово?"'),
        ("создай файл note.txt с текстом 'Version 1.2 released.'", "note.txt", "'Version 1.2 released.'"),
        ("запиши в a.txt с содержимым: 12345", "a.txt", "12345"),
        ("write file hello.txt with content: world?", "hello.txt", "world?"),
    ],
)
def test_imperative_with_content_remains_write(
    goal: str, expected_path: str, expected_content: str
) -> None:
    """Imperative commands with actual content payloads must continue to produce file_write."""
    plan = parse_goal(goal)
    assert plan.intent == "file_write", f"Goal '{goal}' produced intent='{plan.intent}'"
    assert plan.path == expected_path
    assert plan.content == expected_content
