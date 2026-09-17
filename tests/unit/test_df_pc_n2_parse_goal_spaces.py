"""Unit tests for DEFECT N2 / DF-PC-005 space-separated file paths in parse_goal."""

from antigona.task_goal import expected_paths_from_goal, parse_goal


def test_parse_goal_with_spaces_in_path():
    goal = "Создай файл workspace/l15 a b.txt с текстом SPACES"
    plan = parse_goal(goal)
    assert plan.intent == "file_write"
    assert plan.path == "l15 a b.txt"
    assert "l15 a b.txt" in plan.expected_paths
    assert "b.txt" not in plan.expected_paths
    assert plan.content == "SPACES"

    paths = expected_paths_from_goal(goal)
    assert "l15 a b.txt" in paths
    assert "b.txt" not in paths



def test_regression_single_path_no_spaces():
    goal = "Создай файл workspace/l4_seq.txt"
    plan = parse_goal(goal)
    assert plan.intent == "file_write"
    assert plan.path == "l4_seq.txt"
    assert "l4_seq.txt" in plan.expected_paths


def test_read_and_etc_passwd_regression():
    goal_passwd = "Прочитай файл /etc/passwd"
    plan_passwd = parse_goal(goal_passwd)
    assert plan_passwd.intent == "file_read"
    assert plan_passwd.path == "/etc/passwd"
    assert "/etc/passwd" in plan_passwd.expected_paths

    goal_quoted = 'Создай файл "my spaced file.txt" с текстом HELLO'
    plan_quoted = parse_goal(goal_quoted)
    assert plan_quoted.path == "my spaced file.txt"
    assert "my spaced file.txt" in plan_quoted.expected_paths
