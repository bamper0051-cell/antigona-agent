"""BAM-6 regression: aggregate-success/finalizer must NOT emit DONE when a
compound goal's required operations are incomplete.

Repro: a task whose goal names TWO required files (multi_file/compound intent)
is verified after only ONE artifact was produced. The verifier must reject
(not DONE) — otherwise a partially-performed task reports success.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.models import Artifact, TaskState
from antigona.repository import CreateTask, TaskRepository
from antigona.verifier import VerifierCriteriaDatabase, VerifierCriteriaStore
from antigona.verifier.judge import ProviderResult
from tests.unit.test_verifier_v2 import FakeProvider, run_verify


def _build_compound_task(tmp_path: Path) -> tuple[str, str, Path]:
    """Task goal names two files; only ONE artifact/file was produced."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/a.txt",
                "ONE",
                "idem-bam6",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        # Only the FIRST required artifact exists (second op never ran).
        data = b"ONE"
        (workspace / "evidence" / "a.txt").parent.mkdir(parents=True, exist_ok=True)
        (workspace / "evidence" / "a.txt").write_bytes(data)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/a.txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                evidence={"sha256": hashlib.sha256(data).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    return url, task_id, workspace


def test_bam6_incomplete_compound_goal_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DONE is forbidden when a compound goal's required operations are partial."""
    url, task_id, workspace = _build_compound_task(tmp_path)
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    # BAM-6 invariant: partial != DONE. Verifier must NOT finalize.
    assert payload["decision"] != "DONE", payload


def test_bam6_complete_compound_goal_stays_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BOTH required files exist, the compound goal legitimately finalizes DONE."""
    url = f"sqlite:///{tmp_path / 'db2.sqlite'}"
    workspace = tmp_path / "workspace2"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/a.txt",
                "ONE",
                "idem-bam6-ok",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        for name, val in (("a.txt", b"ONE"), ("b.txt", b"TWO")):
            (workspace / "evidence" / name).write_bytes(val)
            session.add(
                Artifact(
                    task_id=task.id,
                    step_id=task.steps[0].id,
                    path=f"evidence/{name}",
                    sha256=hashlib.sha256(val).hexdigest(),
                    size=len(val),
                    evidence={"sha256": hashlib.sha256(val).hexdigest()},
                )
            )
        summary = b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n"
        (workspace / "evidence" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload

def test_bam6_shell_multi_file_incomplete_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real BAM-6 path: CLI goal parses to multi_file → sandbox.shell.
    Only ONE of two required files exists → must NOT finalize DONE."""
    url = f"sqlite:///{tmp_path / 'db4.sqlite'}"
    workspace = tmp_path / "workspace4"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/agg_ok.txt с текстом OK, затем evidence/nonexistent_dir_xyz/agg_bad.txt с текстом BAD"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/summary.txt",
                "evidence/agg_ok.txt: OK\nevidence/nonexistent_dir_xyz/agg_bad.txt: BAD\n",
                "idem-bam6-shell",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        # Only the first required file exists; the second op never ran.
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        (workspace / "evidence" / "agg_ok.txt").write_bytes(b"OK")
        # Shell stdout artifact (summary) exists — but the compound is partial.
        summary = b"evidence/agg_ok.txt: OK\n"
        (workspace / "evidence" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_quoted_content_single_file_not_compound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-regression: a single-file goal whose CONTENT contains a dot must
    NOT be treated as a compound task (Claude review finding: 'Version 1.2'
    was miscounted as an extra path)."""
    url = f"sqlite:///{tmp_path / 'db5.sqlite'}"
    workspace = tmp_path / "workspace5"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = 'Создай report.txt с текстом "Version 1.2 released"'
        task, _ = repo.create(
            CreateTask("owner", goal, "report.txt", "Version 1.2 released", "idem-bam6-q", tool_name="workspace.write_text")
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        data = b"Version 1.2 released"
        (workspace / "report.txt").write_bytes(data)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="report.txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                evidence={"sha256": hashlib.sha256(data).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "report must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload


def test_bam6_folder_multi_file_complete_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folder multi_file (files under folder/, paths in plan are bare names):
    all required files exist → DONE (no false REPLAN)."""
    url = f"sqlite:///{tmp_path / 'db6.sqlite'}"
    workspace = tmp_path / "workspace6"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай папку test с файлами a.txt со значением ONE и b.txt со значением TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "test/summary.txt",
                "a.txt: ONE\nb.txt: TWO\n",
                "idem-bam6-folder-ok",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "test").mkdir(parents=True, exist_ok=True)
        (workspace / "test" / "a.txt").write_bytes(b"ONE")
        (workspace / "test" / "b.txt").write_bytes(b"TWO")
        summary = b"a.txt: ONE\nb.txt: TWO\n"
        (workspace / "test" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="test/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload


def test_bam6_folder_multi_file_incomplete_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folder multi_file: one required file missing under folder/ → NOT DONE."""
    url = f"sqlite:///{tmp_path / 'db7.sqlite'}"
    workspace = tmp_path / "workspace7"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай папку test с файлами a.txt со значением ONE и b.txt со значением TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "test/summary.txt",
                "a.txt: ONE\nb.txt: TWO\n",
                "idem-bam6-folder-bad",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "test").mkdir(parents=True, exist_ok=True)
        (workspace / "test" / "a.txt").write_bytes(b"ONE")
        # b.txt MISSING
        summary = b"a.txt: ONE\nb.txt: TWO\n"
        (workspace / "test" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="test/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_inworkspace_symlink_not_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink inside the workspace pointing at another workspace file must
    NOT count as a really-written required file (anti reward-hacking)."""
    url = f"sqlite:///{tmp_path / 'db8.sqlite'}"
    workspace = tmp_path / "workspace8"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/summary.txt",
                "evidence/a.txt: ONE\nevidence/b.txt: TWO\n",
                "idem-bam6-symlink",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        (workspace / "evidence" / "a.txt").write_bytes(b"ONE")
        (workspace / "evidence" / "b.txt").symlink_to(workspace / "evidence" / "a.txt")
        summary = b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n"
        (workspace / "evidence" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_file_write_read_external_read_target_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_write_read where the READ side points outside the workspace
    (e.g. 'прочитай /etc/hostname и создай report.txt') must still finalize
    DONE when the WRITE target exists (review finding B2)."""
    url = f"sqlite:///{tmp_path / 'db9.sqlite'}"
    workspace = tmp_path / "workspace9"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Прочитай /etc/hostname и создай report.txt с текстом DONE"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "report.txt",
                "DONE",
                "idem-bam6-fwr",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        data = b"DONE"
        (workspace / "report.txt").write_bytes(data)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="report.txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                evidence={"sha256": hashlib.sha256(data).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "report must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload


def test_bam6_hardlink_not_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hardlink (st_nlink>1) to existing content must NOT satisfy the
    required-file gate (review finding H1)."""
    url = f"sqlite:///{tmp_path / 'db10.sqlite'}"
    workspace = tmp_path / "workspace10"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/summary.txt",
                "evidence/a.txt: ONE\nevidence/b.txt: TWO\n",
                "idem-bam6-hardlink",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        (workspace / "evidence" / "a.txt").write_bytes(b"ONE")
        # b.txt is a hardlink to a.txt — same inode, nlink=2.
        os.link(workspace / "evidence" / "a.txt", workspace / "evidence" / "b.txt")
        summary = b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n"
        (workspace / "evidence" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_fwr_write_target_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_write_read: 'Прочитай notes.md и создай summary.txt' — parse_goal
    returns the READ path (notes.md) as .path; the authoritative write target
    is task.target_path (summary.txt). Missing write target → NOT DONE."""
    url = f"sqlite:///{tmp_path / 'db11.sqlite'}"
    workspace = tmp_path / "workspace11"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Прочитай notes.md и создай summary.txt"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "summary.txt",
                "summary",
                "idem-bam6-fwr2",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        # Write target MISSING (notes.md exists, summary.txt does not).
        (workspace / "notes.md").write_bytes(b"n")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="notes.md",
                sha256=hashlib.sha256(b"n").hexdigest(),
                size=1,
                evidence={"sha256": hashlib.sha256(b"n").hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "summary must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_h2_subfolder_not_bare_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: a single-file goal 'a.txt' must NOT be satisfied by an artifact at
    'x/a.txt' (subfolder) — segment-boundary comparison."""
    url = f"sqlite:///{tmp_path / 'db12.sqlite'}"
    workspace = tmp_path / "workspace12"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай a.txt с текстом OK"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "a.txt",
                "OK",
                "idem-bam6-h2",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        # artifact in a subfolder — WRONG location for a bare-name goal.
        (workspace / "x").mkdir()
        (workspace / "x" / "a.txt").write_bytes(b"OK")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="x/a.txt",
                sha256=hashlib.sha256(b"OK").hexdigest(),
                size=2,
                evidence={"sha256": hashlib.sha256(b"OK").hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "a.txt must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_dir_symlink_not_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked PARENT directory must not satisfy the required-file gate."""
    url = f"sqlite:///{tmp_path / 'db13.sqlite'}"
    workspace = tmp_path / "workspace13"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/summary.txt",
                "evidence/a.txt: ONE\nevidence/b.txt: TWO\n",
                "idem-bam6-dirlink",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        (workspace / "evidence" / "a.txt").write_bytes(b"ONE")
        # evidence/b.txt -> real dir outside with b.txt inside
        (workspace / "realb").mkdir()
        (workspace / "realb" / "b.txt").write_bytes(b"TWO")
        (workspace / "evidence" / "b.txt").symlink_to(workspace / "realb" / "b.txt")
        summary = b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n"
        (workspace / "evidence" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both required files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_cli_shaped_fwr_target_path_is_read_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-shaped FWR: CLI sets task.target_path from parse_goal().path which
    is the READ side ('Прочитай notes.md и создай summary.txt' -> path=notes.md).
    The gate must still require the WRITE target (summary.txt) and reject DONE
    when it is missing (grok review finding)."""
    url = f"sqlite:///{tmp_path / 'db14.sqlite'}"
    workspace = tmp_path / "workspace14"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Прочитай notes.md и создай summary.txt"
        # CLI-faithful: target_path == parse_goal().path == notes.md (READ side)
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "notes.md",
                "",
                "idem-bam6-cli-fwr",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "notes.md").write_bytes(b"notes")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="notes.md",
                sha256=hashlib.sha256(b"notes").hexdigest(),
                size=5,
                evidence={"sha256": hashlib.sha256(b"notes").hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "summary must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    # summary.txt missing -> NOT DONE
    assert payload["decision"] != "DONE", payload


def test_bam6_h2_prefixed_artifact_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: artifact at x/evidence/a.txt must NOT satisfy goal path
    evidence/a.txt (prefixed-path bypass)."""
    url = f"sqlite:///{tmp_path / 'db15.sqlite'}"
    workspace = tmp_path / "workspace15"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Создай evidence/a.txt с текстом OK"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/a.txt",
                "OK",
                "idem-bam6-h2p",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "x" / "evidence").mkdir(parents=True, exist_ok=True)
        (workspace / "x" / "evidence" / "a.txt").write_bytes(b"OK")
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="x/evidence/a.txt",
                sha256=hashlib.sha256(b"OK").hexdigest(),
                size=2,
                evidence={"sha256": hashlib.sha256(b"OK").hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "a.txt must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] != "DONE", payload


def test_bam6_cli_shaped_fwr_positive_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive CLI-shaped FWR: target_path = read side, but BOTH the write
    target (summary.txt) and the workspace-local read source (notes.md) exist
    → legitimately DONE (grok nit: missing positive coverage)."""
    url = f"sqlite:///{tmp_path / 'db16.sqlite'}"
    workspace = tmp_path / "workspace16"
    workspace.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        goal = "Прочитай notes.md и создай summary.txt"
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "notes.md",
                "",
                "idem-bam6-cli-fwr-pos",
                tool_name="workspace.write_text",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (workspace / "notes.md").write_bytes(b"notes")
        (workspace / "summary.txt").write_bytes(b"summary")
        for name, val in (("notes.md", b"notes"), ("summary.txt", b"summary")):
            session.add(
                Artifact(
                    task_id=task.id,
                    step_id=task.steps[0].id,
                    path=name,
                    sha256=hashlib.sha256(val).hexdigest(),
                    size=len(val),
                    evidence={"sha256": hashlib.sha256(val).hexdigest()},
                )
            )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "summary must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, workspace, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload


def test_bam6_dir_symlink_parent_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked PARENT directory (evidence -> outside) must not satisfy the
    gate. Either the safe reader rejects (409) or the gate rejects — never DONE."""
    goal = "Создай evidence/a.txt с текстом ONE, затем evidence/b.txt с текстом TWO"
    url = f"sqlite:///{tmp_path / 'db17.sqlite'}"
    ws = tmp_path / "ws17"
    ws.mkdir()
    outside = tmp_path / "outside17"
    outside.mkdir()
    (outside / "a.txt").write_bytes(b"ONE")
    (outside / "b.txt").write_bytes(b"TWO")
    (outside / "summary.txt").write_bytes(b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n")
    (ws / "evidence").symlink_to(outside, target_is_directory=True)
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "evidence/summary.txt",
                "evidence/a.txt: ONE\nevidence/b.txt: TWO\n",
                "idem-bam6-ds17",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        summary = b"evidence/a.txt: ONE\nevidence/b.txt: TWO\n"
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="evidence/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both files must exist")
        criteria_session.commit()
    monkeypatch.setenv("ANTIGONA_WORKSPACE", str(ws))
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    from antigona.verifier.judge import LLMJudge
    from antigona.verifier_service import create_verifier_app
    judge = LLMJudge(primary_model="primary", verifier_model="verifier", provider=provider)
    from fastapi.testclient import TestClient
    with TestClient(create_verifier_app(url, credential="secret", judge=judge)) as client:
        resp = client.post(
            "/verify",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": task_id, "correlation_id": "corr"},
        )
    # 409 (safe reader fail-closed) or decision != DONE — never DONE.
    if resp.status_code == 200:
        assert resp.json().get("decision") != "DONE", resp.json()
    else:
        assert resp.status_code in (409, 422), resp.status_code


def test_bam6_multi_file_external_read_mention_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """multi_file with an external read mention (/etc/hostname) must still
    finalize DONE when all workspace write targets exist."""
    goal = "создай папку data с файлами x.txt и y.txt со значениями 5 и 6, прочитай /etc/hostname"
    url = f"sqlite:///{tmp_path / 'db18.sqlite'}"
    ws = tmp_path / "ws18"
    ws.mkdir()
    db = Database(url)
    db.create_all()
    with db.session_factory() as session:
        repo = TaskRepository(session)
        task, _ = repo.create(
            CreateTask(
                "owner",
                goal,
                "data/summary.txt",
                "x.txt: 5\ny.txt: 6\n",
                "idem-bam6-er18",
                tool_name="sandbox.shell",
            )
        )
        for state in (TaskState.QUEUED, TaskState.PLANNING, TaskState.TOOL_EXECUTING):
            repo.transition(task, state, state.value, "test")
        (ws / "data").mkdir(parents=True, exist_ok=True)
        (ws / "data" / "x.txt").write_bytes(b"5")
        (ws / "data" / "y.txt").write_bytes(b"6")
        summary = b"x.txt: 5\ny.txt: 6\n"
        (ws / "data" / "summary.txt").write_bytes(summary)
        session.add(
            Artifact(
                task_id=task.id,
                step_id=task.steps[0].id,
                path="data/summary.txt",
                sha256=hashlib.sha256(summary).hexdigest(),
                size=len(summary),
                evidence={"sha256": hashlib.sha256(summary).hexdigest()},
            )
        )
        repo.transition(task, TaskState.OBSERVING, "observing", "test")
        repo.transition(task, TaskState.VERIFYING, "verifying", "test")
        repo.commit()
        task_id = task.id
    criteria_db = VerifierCriteriaDatabase(url)
    criteria_db.create_all()
    with criteria_db.session_factory() as criteria_session:
        VerifierCriteriaStore(criteria_session).put(task_id, "both files must exist")
        criteria_session.commit()
    provider = FakeProvider(result=ProviderResult(approved=True, reason="ok", actual_model="verifier"))
    payload = run_verify(url, task_id, ws, provider, monkeypatch)
    assert payload["decision"] == "DONE", payload
