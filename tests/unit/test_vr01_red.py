"""R1-B01 / VR-01 RED evidence and hardening regression.

Candidate ``ca6a8fc2`` finalized a ``file_write_fix_run`` rerun stdout artifact as
DONE on structural evidence alone (hash + non-empty), so a rerun carrying the
WRONG answer — or one that still errored — was reported DONE.

Commit ``6163e3b3`` added a semantic postcondition (expected-token substring).
The follow-up hardening in this branch:

* broadens expectation extraction beyond the single canonical phrasing;
* anchors the value match so ``15`` is not satisfied by ``150`` / ``-15``;
* rejects a rerun whose artifact carries a non-zero ``exit code`` trailer.

Production mutation caught: removing / weakening any of the three fail-closed
checks makes at least one case below finalize DONE again.
"""

from __future__ import annotations

import pytest

from antigona.verifier_service import (
    _fix_run_exit_code,
    _fix_run_expected_token,
    _fix_run_stdout_satisfies,
)
from tests.unit.test_l7_diagnostic_fix_run import FIXED_SOURCE, _verify_fix_run


def test_fix_run_wrong_rerun_stdout_never_becomes_done(tmp_path, monkeypatch) -> None:
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        stdout=b"stdout:\n10\n\nexit code:\n0\n",
    )

    assert payload["decision"] != "DONE", payload


def test_fix_run_superstring_of_expected_value_never_becomes_done(
    tmp_path, monkeypatch
) -> None:
    # Goal expects 15; rerun prints 150. A bare substring check would pass.
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        stdout=b"stdout:\n150\n\nexit code:\n0\n",
    )

    assert payload["decision"] != "DONE", payload


def test_fix_run_nonzero_exit_code_never_becomes_done(tmp_path, monkeypatch) -> None:
    # Rerun prints the expected token but still exits non-zero -> not resolved.
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        stdout=b'Traceback (most recent call last):\n  File "x.py", line 15\n'
        b"ZeroDivisionError: division by zero\n\nexit code:\n1\n",
    )

    assert payload["decision"] != "DONE", payload


def test_fix_run_correct_rerun_stdout_still_becomes_done(tmp_path, monkeypatch) -> None:
    # Regression guard: the hardening must not over-reject the happy path.
    payload = _verify_fix_run(
        tmp_path,
        monkeypatch,
        FIXED_SOURCE,
        stdout=b"stdout:\n15\n\nexit code:\n0\n",
    )

    assert payload["decision"] == "DONE", payload


@pytest.mark.parametrize(
    "goal",
    [
        "пойми почему результат неверный (ожидается 15)",
        "ожидается: 15",
        "ожидается результат 15",
        "ожидается результат: 15",
        "результат должен равняться 15",
        "в итоге программа должна вывести 15",
        "правильный ответ — 15",
        "should print 15",
        "the script should output 15",
        "expected result 15",
    ],
)
def test_expected_token_extraction_covers_natural_phrasings(goal: str) -> None:
    assert _fix_run_expected_token(goal) == "15", goal


@pytest.mark.parametrize(
    "goal",
    [
        "",
        "исправь синтаксическую ошибку и перезапусти",
        "почему падает, почини и покажи вывод",
    ],
)
def test_expected_token_absent_when_no_expectation_stated(goal: str) -> None:
    assert _fix_run_expected_token(goal) is None, goal


@pytest.mark.parametrize(
    ("stdout", "token", "ok"),
    [
        ("stdout:\n15\n\nexit code:\n0\n", "15", True),
        ("total = 15\n", "15", True),
        ("stdout:\n150\n", "15", False),
        ("stdout:\n1523\n", "15", False),
        ("stdout:\n-15\n", "15", False),
        ("stdout:\n-5\n", "5", False),
        ('  File "x.py", line 15\n', "15", True),  # only the exit-code floor stops this
    ],
)
def test_stdout_value_anchor(stdout: str, token: str, ok: bool) -> None:
    assert _fix_run_stdout_satisfies(stdout, token) is ok, (stdout, token)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("stdout:\n15\n\nexit code:\n0\n", 0),
        ("stdout:\nboom\n\nexit code:\n1\n", 1),
        ("exit code: 137\n", 137),
        ("just some stdout with no trailer\n", None),
    ],
)
def test_exit_code_parse(text: str, code: int | None) -> None:
    assert _fix_run_exit_code(text) == code
