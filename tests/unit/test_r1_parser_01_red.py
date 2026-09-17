"""R1-PARSER-01 RED — greedy exact-JSON extraction + interrogative-to-write.

From the R1 blocker inventory (`evidence/hermes_autonomy/T0021`):

* `создай файл a.json и запиши туда ровно {"a":1} а потом ещё {x}` is parsed with
  content `{"a":1} а потом ещё {x}` — the `ровно {…}` capture is greedy and
  unvalidated, so trailing prose is swallowed into the file body.
* `можно создать файл?` / `создай файл?` are parsed as `intent="file_write"` — a
  capability question opens a blind write flow.

Both must be corrected without regressing the already-fixed literal-content
behaviour (explicit content, JSON with dots, sentence punctuation, numbers,
intentional empty file, Unicode).
"""

from __future__ import annotations

import pytest

from antigona.task_goal import parse_goal


def test_exact_json_content_is_bounded_and_valid() -> None:
    plan = parse_goal(
        'создай файл a.json и запиши туда ровно {"a":1} а потом ещё {x}'
    )
    assert plan.intent == "file_write"
    assert plan.path == "a.json"
    # only the bounded, valid JSON object — trailing prose is not file content
    assert plan.content == '{"a":1}'


@pytest.mark.parametrize(
    "goal",
    [
        "можно создать файл?",
        "создай файл?",
        "можешь создать файл test.txt?",
        "не мог бы ты создать файл report.txt?",
    ],
)
def test_interrogative_request_is_not_a_blind_write(goal: str) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "dialog", (goal, plan)


@pytest.mark.parametrize(
    ("goal", "expected_content"),
    [
        ('создай файл a.json и запиши туда ровно {"a":1}', '{"a":1}'),
        ('создай файл note.txt с текстом "Version 1.2 released."',
         '"Version 1.2 released."'),
        ("создай файл count.txt и запиши туда 42", "42"),
        ("создай пустой файл empty.txt", ""),
        ('создай файл r.txt с текстом "Готово?"', '"Готово?"'),
        ("создай файл u.txt с текстом «Привет, мир»", "«Привет, мир»"),
    ],
)
def test_protected_write_behaviour_unchanged(goal: str, expected_content: str) -> None:
    plan = parse_goal(goal)
    assert plan.intent == "file_write", (goal, plan)
    assert plan.content == expected_content, (goal, plan)
