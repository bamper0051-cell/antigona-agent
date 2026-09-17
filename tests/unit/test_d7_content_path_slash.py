r"""D7 content-extraction RED/GREEN test: goals whose path contains a slash (/).

Regression: _CONTENT_WITH_RE used [\w.\-]+ for the path token, which does NOT
match paths containing "/" (e.g. evidence/test_l2_01.txt), so content came back
EMPTY -> worker wrote the whole goal text -> verifier structural check
artifact.size == len(content) passed on the wrong bytes -> false DONE.
Also guards the literal-content invariant: «содержимым/текстом/строго:»
instruction words are stripped, the literal is preserved with no stray prefix.
"""

from antigona.task_goal import parse_goal


def test_content_with_path_containing_slash():
    plan = parse_goal(
        "Создай файл evidence/test_l2_01.txt с содержимым HELLO_ANTIGONA"
    )
    assert plan.intent == "file_write", plan
    assert plan.content == "HELLO_ANTIGONA", repr(plan.content)


def test_content_with_path_slash_text():
    plan = parse_goal("Создай evidence/report.txt с текстом READY")
    assert plan.content == "READY", repr(plan.content)


def test_content_with_path_slash_unicode():
    plan = parse_goal(
        "Создай файл evidence/unicode.txt с текстом Привет, Антигона — тест №42 🚀"
    )
    assert plan.content == "Привет, Антигона — тест №42 🚀", repr(plan.content)


def test_content_repro3_strict():
    plan = parse_goal(
        "Создай файл evidence/repro3.txt с текстом строго: HELLO REPRO3"
    )
    assert plan.content == "HELLO REPRO3", repr(plan.content)
