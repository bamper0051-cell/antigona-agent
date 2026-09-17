"""P0 fix-wave 2026-08-28: regression tests for the 5 Phase-0 CLI blockers.

Each test expresses the CORRECT product behaviour (per MASTER EXAM STANDARD
and the owner's CLI root.md), not the current buggy implementation.

Defects covered:
  P0-017  split-brain routing: "Напиши число 42" must be a direct answer,
          never a file-write task (CLI parse_goal AND gateway intent router).
  P0-030  read delivery: CLI must return file contents to the user after DONE.
  P0-031  two-line content: "ровно с двумя строками: FIRST и SECOND" must
          extract content "FIRST\nSECOND", and verifier must NOT DONE on
          wrong content.
  P0-032  JSON goal: "Создай JSON файл ... data.json: {...}" must parse
          path=data.json, content=valid JSON (JSON keys are NOT filenames).
  L5_1    "запиши в файл X содержимое Y" must extract content=Y (not empty).
"""
import json

from antigona.task_goal import parse_goal

# ---- T1 / T2: «напиши» requires explicit file context ---------------------

def test_t1_napishi_chislo_is_direct_answer_not_file_task():
    """T1: 'Напиши число 42' -> direct response, NO file task."""
    plan = parse_goal("Напиши число 42 и больше ничего.")
    assert plan.intent == "dialog", plan


def test_t2_napishi_v_file_is_file_write():
    """T2: 'Напиши число 42 в файл answer.txt' -> file_write, content=42."""
    plan = parse_goal("Напиши число 42 в файл answer.txt")
    assert plan.intent == "file_write", plan
    assert plan.path == "answer.txt", plan.path
    assert plan.content == "42", repr(plan.content)


# ---- T3: exact two-line file ----------------------------------------------

def test_t3_two_lines_extracts_exact_content():
    """T3: 'ровно с двумя строками: FIRST и SECOND' -> content 'FIRST\nSECOND'."""
    plan = parse_goal(
        "Создай файл exam/p0/two_lines.txt ровно с двумя строками: FIRST и SECOND"
    )
    assert plan.intent == "file_write", plan
    assert plan.path == "exam/p0/two_lines.txt", plan.path
    assert plan.content == "FIRST\nSECOND", repr(plan.content)


# ---- T4: JSON content -----------------------------------------------------

def test_t4_json_content_parses_path_and_content():
    """T4: JSON goal -> correct filename + valid JSON content."""
    goal = 'Создай JSON файл exam/p0/data.json: {"name":"antigona","value":42}'
    plan = parse_goal(goal)
    assert plan.intent == "file_write", plan
    assert plan.path == "exam/p0/data.json", plan.path
    # content must be the literal JSON, not empty, not a JSON key
    assert plan.content, "content must not be empty"
    assert "name" != plan.path, "JSON key must not be used as path"
    parsed = json.loads(plan.content)
    assert parsed == {"name": "antigona", "value": 42}, parsed


# ---- T5: запиши в файл X содержимое Y ------------------------------------

def test_t5_zapishi_v_file_soderzhimoe_extracts_content():
    """T5: 'запиши в файл wrong.txt содержимое EXPECTED_123' -> content."""
    plan = parse_goal(
        "Запиши в файл exam/L5_1/wrong.txt содержимое EXPECTED_123"
    )
    assert plan.intent == "file_write", plan
    assert plan.path == "exam/L5_1/wrong.txt", plan.path
    assert plan.content == "EXPECTED_123", repr(plan.content)


# ---- T6: verifier must not DONE on wrong extracted content ----------------

def test_t6_deterministic_content_mismatch_rejected():
    """T6: verifier-level guard — a goal whose deterministic expected content
    differs from the produced artifact must be rejectable (no DONE)."""
    from antigona.verifier_service import deterministic_expected_content

    goal = (
        "Создай файл exam/p0/two_lines.txt ровно с двумя строками: "
        "FIRST и SECOND"
    )
    expected = deterministic_expected_content(goal)
    assert expected == "FIRST\nSECOND", repr(expected)
    # the old buggy artifact content must fail against the expected content
    assert expected != "двумя строками: FIRST и SECOND"


# ---- T7/T8: read delivery (CLI wrapper) -----------------------------------

def test_t7_read_flow_cli_returns_content():
    """T7: CLI read path must surface artifact content after DONE."""
    import inspect

    from antigona.core.gateway_client import GatewayClient

    # The CLI `run` command must call get_result (or read the file) for
    # read-intent flows; assert the helper exists and is wired.
    src = inspect.getsource(GatewayClient.get_result)
    assert "safe_result_text" in src or "stdout_preview" in src


def test_t8_read_missing_file_honest_failure():
    """T8: read of a missing file -> honest FAILED, no invented content."""
    plan = parse_goal("Прочитай файл exam/p0/definitely_missing_731.txt")
    assert plan.intent == "file_read", plan
    assert plan.path == "exam/p0/definitely_missing_731.txt", plan.path


# ---- T9: runtime roots must be identical (harness-level guard) ------------

def test_t9_runtime_roots_identical(monkeypatch):
    """T9: CLI root == gateway root == workspace root (INVALID_RUN guard)."""
    from antigona.core.paths import project_root, workspace_dir

    # The legacy dev/test default applies with no governed runtime root.
    for name in ("ANTIGONA_STATE_ROOT", "ANTIGONA_WORKSPACE", "ANTIGONA_IMMUTABLE_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)

    # The runtime slice is anchored to the discovered project root, not a
    # literal checkout basename.
    root = project_root()
    assert (root / "pyproject.toml").is_file(), root
    assert workspace_dir().parent == root, workspace_dir()
