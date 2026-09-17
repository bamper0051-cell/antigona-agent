"""T-содержимым: parse_goal extracts content from 'с содержимым' pattern."""
from antigona.task_goal import parse_goal


def test_soderzhimym_basic():
    """с содержимым extracts content."""
    g = parse_goal("Создай файл e2e_test.txt в workspace с содержимым FULL E2E PASS")
    assert g.content == "FULL E2E PASS"
    assert g.path == "e2e_test.txt"


def test_soderzhimym_with_readback():
    """с содержимым + После записи → read_after_write=True, content trimmed."""
    g = parse_goal(
        "Создай файл e2e_test.txt с содержимым FULL E2E PASS. "
        "После записи прочитай файл обратно и покажи путь, инструмент записи, "
        "инструмент чтения и точное содержимое."
    )
    assert g.content == "FULL E2E PASS"
    assert g.read_after_write is True


def test_tekstom_still_works():
    """Existing 'с текстом' pattern must not regress."""
    g = parse_goal("Создай файл hello.txt с текстом Hello World")
    assert g.content == "Hello World"

