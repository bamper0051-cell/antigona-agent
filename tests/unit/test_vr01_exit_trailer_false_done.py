"""VR-01 / P1-01 (Antigona R2 Codex review): the ``exit code`` trailer must not
satisfy a ``file_write_fix_run`` postcondition.

Canonical finding — ``evidence/hermes_autonomy/T0031/CODEX_REVIEW_CANONICAL.md``:

    src/antigona/verifier_service.py:779-790,855-862 — При цели с ``ожидается 0``
    rerun может вывести ``999`` и завершиться с кодом 0: matcher находит
    ожидаемый ``0`` в служебном trailer ``exit code: 0``, после чего structural
    path переводит flow в DONE — доказуемый false DONE — тесты используют только
    ожидание ``15``, не совпадающее с exit-code trailer — blocking

Mechanism: the orchestrator materializes a rerun as
``stdout:\\n<preview>\\n\\nexit code:\\n<n>\\n`` (``orchestrator.py:485``).
``_fix_run_stdout_satisfies`` searches that whole blob for the expected token as
a standalone value. When the goal's stated expectation is ``0`` (the value the
orchestrator itself writes into every clean ``exit code:\\n0`` trailer), the
trailer's ``0`` satisfies the postcondition even though the corrected script
still prints ``999`` — the verifier grants a structural DONE for an unresolved
fix-run.

The pre-existing suite (``test_vr01_red.py``) only ever expects ``15``, which
never coincides with the exit-code trailer, so this collision is unexercised.

These tests drive the real verifier service (``POST /verify`` via
``run_verify``), not the internal helper, because the false DONE is granted at
the service boundary.
"""

from __future__ import annotations

import hashlib
from typing import Any

from antigona.database import Database
from antigona.models import Artifact, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore
from antigona.verifier.judge import ProviderResult
from tests.unit.test_verifier_v2 import FakeProvider, run_verify

# A fix-run goal whose stated expectation is 0 — the exact value the orchestrator
# also writes into the "exit code:\n0" trailer of every clean rerun.
ZERO_GOAL = (
    "Создай buggy_ZERO_7f3a.py: def f():\n"
    "    return 999\n"
    "print(f())\n"
    "Запусти его, пойми почему результат неверный (ожидается 0), исправь код, "
    "перезапусти и покажи исправленный вывод."
)
BUGGY_SOURCE = "def f():\n    return 999\nprint(f())\n"
# The "fixed" source is still wrong: the rerun keeps printing 999, exit code 0.
STILL_WRONG_SOURCE = "def f():\n    return 999  # tried, still wrong\nprint(f())\n"


def _verify_zero_goal_fix_run(
    tmp_path, monkeypatch, *, stdout: bytes
) -> dict[str, Any]:
    url = f"sqlite:///{tmp_path / 'verify.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                ZERO_GOAL,
                "buggy_ZERO_7f3a.py",
                BUGGY_SOURCE,
                "idem-vr01-zero",
                tool_name="workspace.write_text",
                run_after_write=True,
                run_command=("python", "buggy_ZERO_7f3a.py"),
                fix_after_run=True,
                fix_content=STILL_WRONG_SOURCE,
                fix_command=("python", "buggy_ZERO_7f3a.py"),
            )
        )
        for st in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, st, st.value, "test")
        (workspace / "buggy_ZERO_7f3a.py").write_bytes(STILL_WRONG_SOURCE.encode("utf-8"))
        res_path = f".antigona-results/{task.id}.txt"
        (workspace / ".antigona-results").mkdir(parents=True, exist_ok=True)
        (workspace / res_path).write_bytes(stdout)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path=res_path,
                sha256=hashlib.sha256(stdout).hexdigest(),
                size=len(stdout),
                evidence={"sha256": hashlib.sha256(stdout).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id

    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "script exists and prints 0")
        criteria_session.commit()

    provider = FakeProvider(
        result=ProviderResult(approved=True, reason="ok", actual_model="verifier")
    )
    return run_verify(url, task_id, workspace, provider, monkeypatch)


def test_exit_code_trailer_zero_does_not_satisfy_fix_run_postcondition(
    tmp_path, monkeypatch
) -> None:
    # Rerun still prints 999 (defect unresolved) but exits 0. The only standalone
    # "0" anywhere in the artifact is the orchestrator's "exit code:\n0" trailer.
    payload = _verify_zero_goal_fix_run(
        tmp_path, monkeypatch, stdout=b"stdout:\n999\n\nexit code:\n0\n"
    )

    assert payload["decision"] != "DONE", payload


def test_zero_goal_fix_run_still_done_when_rerun_actually_prints_zero(
    tmp_path, monkeypatch
) -> None:
    # Regression guard for the fix: a genuine rerun output of 0 in the stdout
    # section must still finalize DONE — the fix must not over-reject.
    payload = _verify_zero_goal_fix_run(
        tmp_path, monkeypatch, stdout=b"stdout:\n0\n\nexit code:\n0\n"
    )

    assert payload["decision"] == "DONE", payload
