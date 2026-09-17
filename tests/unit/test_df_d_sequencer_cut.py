"""DF-D partial-fail propagation: sequencer «затем» must not pollute write content."""
from antigona.task_goal import _cut_sentence, _extract_content, parse_goal


def test_sequencer_does_not_pollute_content():
    g = "Создай evidence/before_failure.txt с текстом BEFORE, затем прочитай файл, которого не существует."
    assert _extract_content(g) == "BEFORE"


def test_sequencer_cut_min():
    assert _cut_sentence("BEFORE, затем прочитай файл") == "BEFORE"
    assert _cut_sentence("одно, потом другое") == "одно"
    assert _cut_sentence("первое после этого второе") == "первое"


def test_normal_sentence_extract_unchanged():
    assert _extract_content("Создай файл x.txt с текстом один.") == "один"


def test_same_path_read_back_kept():
    g = "Создай evidence/a.txt с текстом HELLO и прочитай его"
    p = parse_goal(g)
    assert p.intent == "file_write_read"
