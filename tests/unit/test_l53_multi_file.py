# RED->GREEN: L5-3 multi-file compound — "file1 с текстом X и file2 с текстом Y"
# must decompose into a multi_file plan with BOTH files + both contents.
from antigona.task_goal import _build_multi_file_command, _extract_multi_file, parse_goal


def test_l53_multi_file_two_files_decomposes():
    g = "Создай evidence/p1.txt с текстом PART1 и evidence/p2.txt с текстом PART2"
    multi = _extract_multi_file(g)
    assert multi is not None, "multi_file should be detected without the word 'папка'"
    files = multi["files"]
    pairs = multi["pairs"]
    assert "evidence/p1.txt" in files and "evidence/p2.txt" in files, files
    contents = {n: v for n, v in pairs}
    assert contents.get("evidence/p1.txt") == "PART1", contents
    assert contents.get("evidence/p2.txt") == "PART2", contents

def test_l53_parse_goal_intent_multi_file():
    g = "Создай evidence/p1.txt с текстом PART1 и evidence/p2.txt с текстом PART2"
    p = parse_goal(g)
    assert p.intent == "multi_file", p.intent
    # content is the legitimate summary, must contain both pairs (not polluted raw remainder)
    assert "PART1" in p.content and "PART2" in p.content, p.content
    assert p.path == "evidence/summary.txt", p.path

def test_l53_build_command_writes_both_correct_paths():
    g = "Создай evidence/p1.txt с текстом PART1 и evidence/p2.txt с текстом PART2"
    multi = _extract_multi_file(g)
    cmd, path, content = _build_multi_file_command(multi)
    assert "> evidence/p1.txt" in cmd, cmd
    assert "> evidence/p2.txt" in cmd, cmd
    assert "evidence/evidence/" not in cmd, cmd  # no double prefix
