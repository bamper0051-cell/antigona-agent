"""Unit tests for antigona.task_goal (D7/D8/D10 extraction layer)."""

from antigona.task_goal import expected_paths_from_goal, parse_goal

FS_REGRESSION = (
    'Создай файл с именем exactly "manual test.txt" в /workspace. '
    "Содержимое ровно две строки: ANTIGONA FILE TEST / 12345. Затем: "
    "- выполни запись ровно один раз - прочитай тот же путь ровно один раз "
    "- верни: точный путь, содержимое, инструмент записи, инструмент чтения. "
    "Выполни всю задачу за один проход."
)

LITERAL = (
    "Создай файл literal.txt. Содержимое ровно двумя строками: "
    "ANTIGONA FILE TEST / 12345"
)

T30 = (
    "Создай папку antigona_manual_test. В ней создай три файла: one.txt, "
    "two.txt, three.txt со значениями ONE, TWO, THREE. Затем прочитай все "
    "три. Создай summary.txt со строками <filename>: <content>. Прочитай "
    "summary и покажи полный результат."
)

T36 = (
    "Выполни одну задачу: создай delivery_once.txt с ONE RESULT. "
    "Финальный результат доставь ровно один раз."
)

L10 = (
    "Создай папку my_project с подпапками src и tests; в src создай main.py, "
    "в tests создай test_main.py"
)

L06A = "Прочитай файл /etc/passwd"
L06C = "Прочитай файл ../../../etc/passwd"
L03 = "Прочитай файл literal.txt и верни его содержимое"
ECHO = "Выполни echo hello"
CAT = "Выполни команду cat /etc/passwd"
TWO_PLUS_TWO = "Сколько будет 2+2?"
HELLO = "привет"
MODEL = "Какая модель тебя обслуживает?"
SAY_ONE = "Скажи слово один"
GREETING = "Напиши короткое приветствие на русском языке"
REPORT = "Создай отчёт report.md: заголовок ОТЧЁТ и три пункта: один, два, три"
DURABLE = "Создай файл durable_test.txt с текстом DURABLE OK"
L08 = "Создай отчёт report.md: заголовок ОТЧЁТ и три пункта: один, два, три"


def test_fs_regression_write_read():
    plan = parse_goal(FS_REGRESSION)
    assert plan.intent == "file_write_read", plan
    assert plan.path == "manual test.txt", plan.path
    assert plan.content == "ANTIGONA FILE TEST\n12345", repr(plan.content)
    assert plan.read_after_write is True
    assert "manual test.txt" in plan.expected_paths


def test_literal_write():
    plan = parse_goal(LITERAL)
    assert plan.intent == "file_write", plan
    assert plan.path == "literal.txt"
    assert plan.content == "ANTIGONA FILE TEST\n12345"


def test_t36_write():
    plan = parse_goal(T36)
    assert plan.intent == "file_write", plan
    assert plan.path == "delivery_once.txt"
    assert plan.content == "ONE RESULT", repr(plan.content)


def test_t30_multi_file():
    plan = parse_goal(T30)
    assert plan.intent == "multi_file", plan
    assert plan.command
    assert "antigona_manual_test" in plan.command
    assert "printf 'ONE\\n' > antigona_manual_test/one.txt" in plan.command
    assert plan.path == "antigona_manual_test/summary.txt"
    assert plan.content == "one.txt: ONE\ntwo.txt: TWO\nthree.txt: THREE\n"


def test_l10_multi_file_no_values():
    plan = parse_goal(L10)
    assert plan.intent == "multi_file", plan
    assert plan.command
    assert "my_project" in plan.command


def test_read_etc_passwd_path_kept():
    plan = parse_goal(L06A)
    assert plan.intent == "file_read"
    assert plan.path == "/etc/passwd"


def test_read_traversal_path_kept():
    plan = parse_goal(L06C)
    assert plan.intent == "file_read"
    assert plan.path == "../../../etc/passwd"


def test_read_literal():
    plan = parse_goal(L03)
    assert plan.intent == "file_read"
    assert plan.path == "literal.txt"


def test_shell_echo():
    plan = parse_goal(ECHO)
    assert plan.intent == "shell", plan
    assert plan.command == "echo hello", plan.command


def test_shell_cat():
    plan = parse_goal(CAT)
    assert plan.intent == "shell", plan
    assert plan.command == "cat /etc/passwd", plan.command


def test_dialog_two_plus_two():
    plan = parse_goal(TWO_PLUS_TWO)
    assert plan.intent == "dialog", plan


def test_dialog_hello():
    plan = parse_goal(HELLO)
    assert plan.intent == "dialog", plan


def test_dialog_model():
    plan = parse_goal(MODEL)
    assert plan.intent == "dialog", plan


def test_dialog_say_one():
    plan = parse_goal(SAY_ONE)
    assert plan.intent == "dialog", plan


def test_dialog_greeting():
    plan = parse_goal(GREETING)
    assert plan.intent == "dialog", plan


def test_report_md():
    plan = parse_goal(REPORT)
    assert plan.intent == "file_write", plan
    assert plan.path == "report.md", plan.path
    assert plan.content, "content must not be empty"


def test_durable():
    plan = parse_goal(DURABLE)
    assert plan.intent == "file_write", plan
    assert plan.path == "durable_test.txt"
    assert plan.content == "DURABLE OK"


def test_expected_paths_dedup():
    paths = expected_paths_from_goal(LITERAL)
    assert paths == ["literal.txt"], paths


def test_expected_paths_quoted():
    paths = expected_paths_from_goal(FS_REGRESSION)
    assert "manual test.txt" in paths
