# RED: CLI parser _extract_path must preserve the subdirectory (evidence/ prefix).
import pytest

from antigona.task_goal import parse_goal


@pytest.mark.parametrize("goal,expected_path,expected_content", [
    ("Создай evidence/wrong.txt с текстом EXPECTED_123", "evidence/wrong.txt", "EXPECTED_123"),
    ("Создай evidence/chain1.txt с текстом CHAIN_OK", "evidence/chain1.txt", "CHAIN_OK"),
    ("Создай evidence/test_l2_01.txt с содержимым HELLO_ANTIGONA", "evidence/test_l2_01.txt", "HELLO_ANTIGONA"),
])
def test_path_preserves_subdir(goal, expected_path, expected_content):
    p = parse_goal(goal)
    assert p.path == expected_path, f"path={p.path!r}, expected {expected_path!r}"
    assert p.content == expected_content, f"content={p.content!r}, expected {expected_content!r}"

def test_path_preserves_subdir_nested():
    p = parse_goal("Создай evidence/sandbox/a/b/c/legal.txt с текстом LEGAL")
    assert p.path == "evidence/sandbox/a/b/c/legal.txt", p.path
