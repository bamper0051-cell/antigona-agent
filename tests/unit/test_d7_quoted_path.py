"""D7 content-extraction RED/GREEN test: goals whose path is QUOTED and contains spaces.

_QUOTED_RE correctly extracts the path ('evidence/my test file.txt') but
_CONTENT_WITH_RE requires a space-free path token [word/dot/slash chars] so it fails on
quoted paths -> content comes back EMPTY. Guard that quoted-space paths also
yield their literal content.
"""

from antigona.task_goal import parse_goal


def test_content_with_quoted_space_path():
    plan = parse_goal('Создай файл "evidence/my test file.txt" с текстом SPACE_PATH_OK')
    assert plan.intent == "file_write", plan
    assert plan.path == "evidence/my test file.txt", repr(plan.path)
    assert plan.content == "SPACE_PATH_OK", repr(plan.content)


def test_content_with_quoted_space_path_single_quote():
    plan = parse_goal("Создай файл 'evidence/my test file.txt' с содержимым SPACE_OK2")
    assert plan.path == "evidence/my test file.txt", repr(plan.path)
    assert plan.content == "SPACE_OK2", repr(plan.content)
