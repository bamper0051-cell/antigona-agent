"""P5-008 TaskRuntime Path Fence Green Inverted Audit Test Suite.

Safe audit with tmp_path workspace/outside fixtures and intercepted filesystem calls.
Asserts fail-closed boundary enforcement of TaskRuntime file operations and persistence helpers:
- WRITE_FILE (denies dotdot, absolute, percent, symlink, lexical sibling before filesystem calls)
- READ_FILE (denies dotdot, absolute, percent, symlink, lexical sibling before filesystem calls)
- SEND_FILE (denies dotdot, absolute, percent, symlink, lexical sibling before filesystem calls)
- PERSISTENT_HELPERS (_load_task, _save_task, _delete_task strict allowlist and containment)
- Early primitive boundary call interception spies
- Normal in-workspace operations remain functional
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from antigona.task.runtime import (
    PathBoundaryViolation,
    Task,
    TaskRuntime,
    _delete_task,
    _list_tasks,
    _load_task,
    _save_task,
)


@pytest.fixture
def probe_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fixture providing isolated workspace, outside dir, sibling dir, and persist dir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    sibling = tmp_path / "workspace_sibling"
    sibling.mkdir(parents=True, exist_ok=True)

    persist_dir = tmp_path / "tasks_persist"
    persist_dir.mkdir(parents=True, exist_ok=True)

    # Outside test assets
    outside_secret = outside / "secret.txt"
    outside_secret.write_text("SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5", encoding="utf-8")

    outside_task_file = outside / "outside_task.json"
    outside_task_data = {
        "id": "outside_task",
        "goal": "Outside Task Payload",
        "steps": [],
        "status": "pending",
        "created_at": 1000.0,
        "completed_at": None,
        "current_step_index": 0,
    }
    outside_task_file.write_text(json.dumps(outside_task_data), encoding="utf-8")

    sibling_secret = sibling / "sibling_secret.txt"
    sibling_secret.write_text("SIBLING_SECRET_DATA_WAVE5", encoding="utf-8")

    # Symlink inside workspace pointing outside
    symlink_file = workspace / "symlink_to_outside.txt"
    symlink_file.symlink_to(outside_secret)

    # Symlink directory inside workspace pointing outside
    symlink_dir = workspace / "symlink_dir_to_outside"
    symlink_dir.symlink_to(outside)

    # Symlink inside persist dir pointing to outside task
    persist_symlink = persist_dir / "symlink_task.json"
    persist_symlink.symlink_to(outside_task_file)

    # Fake project root outside workspace
    fake_proj_root = tmp_path / "fake_repo"
    fake_proj_root.mkdir(parents=True, exist_ok=True)

    # Monkeypatch persist dir, project root and task workspace
    monkeypatch.setattr("antigona.task.runtime.TASK_PERSIST_DIR", str(persist_dir))
    monkeypatch.setattr("antigona.core.paths.project_root", lambda: fake_proj_root)
    monkeypatch.setenv("ANTIGONA_TASK_WORKSPACE", str(workspace))

    # Change working directory to workspace
    orig_cwd = os.getcwd()
    os.chdir(workspace)

    yield {
        "tmp_path": tmp_path,
        "workspace": workspace,
        "outside": outside,
        "sibling": sibling,
        "persist_dir": persist_dir,
        "outside_secret": outside_secret,
        "outside_task_file": outside_task_file,
        "sibling_secret": sibling_secret,
        "symlink_file": symlink_file,
        "symlink_dir": symlink_dir,
        "persist_symlink": persist_symlink,
        "fake_proj_root": fake_proj_root,
    }

    os.chdir(orig_cwd)


class TestTaskRuntimePathFenceAudit:
    """Rigorous audit probes for TaskRuntime file actions and persistence helpers."""

    # ──────────────────────────────────────────────────────────────────────────
    # WRITE_FILE Probes
    # ──────────────────────────────────────────────────────────────────────────

    def test_write_file_dotdot_outside(self, probe_fixture: dict[str, Any]) -> None:
        """WRITE_FILE probe 1: dotdot relative traversal is denied fail-closed."""
        runtime = TaskRuntime()
        outside_target = probe_fixture["outside"] / "written_dotdot.txt"
        assert not outside_target.exists()

        step = {
            "action_type": "WRITE_FILE",
            "path": "../outside/written_dotdot.txt",
            "content": "PAYLOAD_WRITE_DOTDOT",
        }
        task = runtime.create_task("Probe WRITE_FILE dotdot", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert not outside_target.exists()

    def test_write_file_absolute_outside(self, probe_fixture: dict[str, Any]) -> None:
        """WRITE_FILE probe 2: absolute path writes are denied fail-closed."""
        runtime = TaskRuntime()
        outside_target = probe_fixture["outside"] / "written_abs.txt"
        assert not outside_target.exists()

        step = {
            "action_type": "WRITE_FILE",
            "path": str(outside_target),
            "content": "PAYLOAD_WRITE_ABS",
        }
        task = runtime.create_task("Probe WRITE_FILE absolute", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert not outside_target.exists()

    def test_write_file_percent_encoded(self, probe_fixture: dict[str, Any]) -> None:
        """WRITE_FILE probe 3: percent-encoded traversal is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "WRITE_FILE",
            "path": "..%2f..%2foutside%2fwritten_enc.txt",
            "content": "PAYLOAD_WRITE_ENC",
        }
        task = runtime.create_task("Probe WRITE_FILE percent encoded", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        literal_file = probe_fixture["workspace"] / "..%2f..%2foutside%2fwritten_enc.txt"
        assert not literal_file.exists()

    def test_write_file_symlink_outside(self, probe_fixture: dict[str, Any]) -> None:
        """WRITE_FILE probe 4: symlink inside workspace is denied before write."""
        runtime = TaskRuntime()
        outside_secret: Path = probe_fixture["outside_secret"]
        assert outside_secret.read_text(encoding="utf-8") == "SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5"

        step = {
            "action_type": "WRITE_FILE",
            "path": "symlink_to_outside.txt",
            "content": "OVERWRITTEN_VIA_SYMLINK",
        }
        task = runtime.create_task("Probe WRITE_FILE symlink", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert outside_secret.read_text(encoding="utf-8") == "SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5"

    def test_write_file_lexical_sibling(self, probe_fixture: dict[str, Any]) -> None:
        """WRITE_FILE probe 5: lexical sibling path write is denied fail-closed."""
        runtime = TaskRuntime()
        sibling_target = probe_fixture["sibling"] / "written_sibling.txt"
        assert not sibling_target.exists()

        step = {
            "action_type": "WRITE_FILE",
            "path": "../workspace_sibling/written_sibling.txt",
            "content": "PAYLOAD_WRITE_SIBLING",
        }
        task = runtime.create_task("Probe WRITE_FILE sibling", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert not sibling_target.exists()

    # ──────────────────────────────────────────────────────────────────────────
    # READ_FILE Probes
    # ──────────────────────────────────────────────────────────────────────────

    def test_read_file_dotdot_outside(self, probe_fixture: dict[str, Any]) -> None:
        """READ_FILE probe 1: dotdot relative traversal is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "READ_FILE",
            "path": "../outside/secret.txt",
        }
        task = runtime.create_task("Probe READ_FILE dotdot", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5" not in str(res)

    def test_read_file_absolute_outside(self, probe_fixture: dict[str, Any]) -> None:
        """READ_FILE probe 2: absolute path read is denied fail-closed."""
        runtime = TaskRuntime()
        outside_secret: Path = probe_fixture["outside_secret"]

        step = {
            "action_type": "READ_FILE",
            "path": str(outside_secret),
        }
        task = runtime.create_task("Probe READ_FILE absolute", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5" not in str(res)

    def test_read_file_percent_encoded(self, probe_fixture: dict[str, Any]) -> None:
        """READ_FILE probe 3: percent-encoded path is denied with PathBoundaryViolation."""
        runtime = TaskRuntime()
        step = {
            "action_type": "READ_FILE",
            "path": "..%2foutside%2fsecret.txt",
        }
        task = runtime.create_task("Probe READ_FILE percent encoded", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_read_file_symlink_outside(self, probe_fixture: dict[str, Any]) -> None:
        """READ_FILE probe 4: symlink inside workspace is denied before read."""
        runtime = TaskRuntime()
        step = {
            "action_type": "READ_FILE",
            "path": "symlink_to_outside.txt",
        }
        task = runtime.create_task("Probe READ_FILE symlink", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "SENSITIVE_OUTSIDE_SECRET_DATA_WAVE5" not in str(res)

    def test_read_file_lexical_sibling(self, probe_fixture: dict[str, Any]) -> None:
        """READ_FILE probe 5: lexical sibling path read is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "READ_FILE",
            "path": "../workspace_sibling/sibling_secret.txt",
        }
        task = runtime.create_task("Probe READ_FILE sibling", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "SIBLING_SECRET_DATA_WAVE5" not in str(res)

    # ──────────────────────────────────────────────────────────────────────────
    # SEND_FILE Probes
    # ──────────────────────────────────────────────────────────────────────────

    def test_send_file_dotdot_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEND_FILE probe 1: dotdot relative traversal is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEND_FILE",
            "path": "../outside/secret.txt",
        }
        task = runtime.create_task("Probe SEND_FILE dotdot", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "готов к отправке" not in str(res.get("message", ""))

    def test_send_file_absolute_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEND_FILE probe 2: absolute path is denied fail-closed."""
        runtime = TaskRuntime()
        outside_secret: Path = probe_fixture["outside_secret"]

        step = {
            "action_type": "SEND_FILE",
            "path": str(outside_secret),
        }
        task = runtime.create_task("Probe SEND_FILE absolute", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "готов к отправке" not in str(res.get("message", ""))

    def test_send_file_percent_encoded(self, probe_fixture: dict[str, Any]) -> None:
        """SEND_FILE probe 3: percent-encoded path is denied with PathBoundaryViolation."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEND_FILE",
            "path": "..%2foutside%2fsecret.txt",
        }
        task = runtime.create_task("Probe SEND_FILE percent encoded", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_send_file_symlink_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEND_FILE probe 4: symlink inside workspace is denied before send."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEND_FILE",
            "path": "symlink_to_outside.txt",
        }
        task = runtime.create_task("Probe SEND_FILE symlink", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "готов к отправке" not in str(res.get("message", ""))

    def test_send_file_lexical_sibling(self, probe_fixture: dict[str, Any]) -> None:
        """SEND_FILE probe 5: lexical sibling path is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEND_FILE",
            "path": "../workspace_sibling/sibling_secret.txt",
        }
        task = runtime.create_task("Probe SEND_FILE sibling", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert "готов к отправке" not in str(res.get("message", ""))

    # ──────────────────────────────────────────────────────────────────────────
    # Persistent Helper Probes (_load_task, _save_task, _delete_task)
    # ──────────────────────────────────────────────────────────────────────────

    def test_persistent_load_task_dotdot(self, probe_fixture: dict[str, Any]) -> None:
        """PERSISTENCE probe 1: _load_task with dotdot task_id fails closed and returns None."""
        task_id = "../outside/outside_task"
        loaded = _load_task(task_id)
        assert loaded is None

    def test_persistent_load_task_absolute(self, probe_fixture: dict[str, Any]) -> None:
        """PERSISTENCE probe 2: _load_task with absolute path fails closed and returns None."""
        outside_task_base = str(probe_fixture["outside"] / "outside_task")
        loaded = _load_task(outside_task_base)
        assert loaded is None

    def test_persistent_load_task_symlink(self, probe_fixture: dict[str, Any]) -> None:
        """PERSISTENCE probe 3: _load_task with symlinked task id fails closed and returns None."""
        loaded = _load_task("symlink_task")
        assert loaded is None

    def test_persistent_delete_task_dotdot(self, probe_fixture: dict[str, Any]) -> None:
        """PERSISTENCE probe 4: _delete_task with dotdot task_id fails closed without deleting."""
        target_file = probe_fixture["outside"] / "deletable_task.json"
        target_file.write_text(json.dumps({"id": "deletable", "goal": "del", "steps": []}))
        assert target_file.exists()

        _delete_task("../outside/deletable_task")

        # Confirmation: Outside file is preserved untouched
        assert target_file.exists()

    def test_persistent_save_task_dotdot(self, probe_fixture: dict[str, Any]) -> None:
        """PERSISTENCE probe 5: _save_task with dotdot task.id raises PathBoundaryViolation."""
        task = Task(
            id="../outside/saved_outside_task",
            goal="Saved Outside",
            steps=[],
            status="pending",
        )
        with pytest.raises(PathBoundaryViolation):
            _save_task(task)

        target_file = probe_fixture["outside"] / "saved_outside_task.json"
        assert not target_file.exists()

    # ──────────────────────────────────────────────────────────────────────────
    # Call Spies / Earliest Boundary Interception Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_write_file_call_spies_deny_before_fs_side_effects(
        self, probe_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Call spies verify rejection occurs BEFORE open/write/mkdir on target."""
        outside_target = probe_fixture["outside"] / "spy_target.txt"
        assert not outside_target.exists()

        intercepted_opens: list[str] = []
        intercepted_mkdirs: list[str] = []

        orig_open = Path.open
        orig_mkdir = Path.mkdir

        def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            intercepted_opens.append(str(self))
            return orig_open(self, *args, **kwargs)

        def spy_mkdir(self: Path, *args: Any, **kwargs: Any) -> Any:
            intercepted_mkdirs.append(str(self))
            return orig_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", spy_open)
        monkeypatch.setattr(Path, "mkdir", spy_mkdir)

        runtime = TaskRuntime()
        step = {
            "action_type": "WRITE_FILE",
            "path": "../outside/spy_target.txt",
            "content": "SPY_BLOCKED",
        }
        task = runtime.create_task("Spy write test", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert not outside_target.exists()
        # Ensure outside target was never opened or created
        assert not any("spy_target.txt" in p for p in intercepted_opens)
        assert not any("outside" in p for p in intercepted_mkdirs)

    def test_read_file_call_spies_deny_before_read(
        self, probe_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Call spies verify read rejection occurs BEFORE read_text/open."""
        outside_secret = probe_fixture["outside_secret"]
        intercepted_reads: list[str] = []

        orig_read_text = Path.read_text

        def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> Any:
            intercepted_reads.append(str(self))
            return orig_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read_text)

        runtime = TaskRuntime()
        step = {
            "action_type": "READ_FILE",
            "path": str(outside_secret),
        }
        task = runtime.create_task("Spy read test", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"
        assert not any("secret.txt" in p for p in intercepted_reads)

    def test_delete_task_call_spies_deny_before_unlink(
        self, probe_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Call spies verify delete rejection occurs BEFORE unlink."""
        outside_file = probe_fixture["outside"] / "spy_delete.json"
        outside_file.write_text(json.dumps({"id": "spy_del", "goal": "g"}))
        assert outside_file.exists()

        intercepted_unlinks: list[str] = []
        orig_unlink = Path.unlink

        def spy_unlink(self: Path, *args: Any, **kwargs: Any) -> Any:
            intercepted_unlinks.append(str(self))
            return orig_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy_unlink)

        _delete_task("../outside/spy_delete")

        assert outside_file.exists()
        assert not any("spy_delete" in p for p in intercepted_unlinks)

    # ──────────────────────────────────────────────────────────────────────────
    # SEARCH_FILES Probes
    # ──────────────────────────────────────────────────────────────────────────

    def test_search_files_dotdot_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEARCH_FILES probe 1: dotdot relative traversal is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEARCH_FILES",
            "content": "*.txt",
            "path": "../outside",
        }
        task = runtime.create_task("Probe SEARCH_FILES dotdot", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_search_files_absolute_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEARCH_FILES probe 2: absolute path search is denied fail-closed."""
        runtime = TaskRuntime()
        outside_path = str(probe_fixture["outside"])
        step = {
            "action_type": "SEARCH_FILES",
            "content": "*.txt",
            "path": outside_path,
        }
        task = runtime.create_task("Probe SEARCH_FILES absolute", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_search_files_percent_encoded(self, probe_fixture: dict[str, Any]) -> None:
        """SEARCH_FILES probe 3: percent-encoded path is denied with PathBoundaryViolation."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEARCH_FILES",
            "content": "*.txt",
            "path": "..%2foutside",
        }
        task = runtime.create_task("Probe SEARCH_FILES percent encoded", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_search_files_symlink_outside(self, probe_fixture: dict[str, Any]) -> None:
        """SEARCH_FILES probe 4: symlink inside workspace is denied before search."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEARCH_FILES",
            "content": "*.txt",
            "path": "symlink_dir_to_outside",
        }
        task = runtime.create_task("Probe SEARCH_FILES symlink", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_search_files_lexical_sibling(self, probe_fixture: dict[str, Any]) -> None:
        """SEARCH_FILES probe 5: lexical sibling path search is denied fail-closed."""
        runtime = TaskRuntime()
        step = {
            "action_type": "SEARCH_FILES",
            "content": "*.txt",
            "path": "../workspace_sibling",
        }
        task = runtime.create_task("Probe SEARCH_FILES sibling", [step])
        res = runtime.execute_next_step(task.id)

        assert res is not None
        assert res["success"] is False
        assert res["error"] == "PathBoundaryViolation"

    def test_search_files_call_spies_zero_subprocess_calls_on_invalid_path(
        self, probe_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Call spies verify zero subprocess/Popen calls when SEARCH_FILES path is invalid."""
        import subprocess

        intercepted_runs: list[list[str]] = []
        intercepted_popens: list[list[str]] = []

        orig_run = subprocess.run
        orig_popen = subprocess.Popen

        def spy_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            intercepted_runs.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
            return orig_run(cmd, *args, **kwargs)

        def spy_popen(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            intercepted_popens.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
            return orig_popen(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy_run)
        monkeypatch.setattr(subprocess, "Popen", spy_popen)

        runtime = TaskRuntime()
        invalid_paths = [
            "../outside",
            str(probe_fixture["outside"]),
            "..%2foutside",
            "symlink_dir_to_outside",
            "../workspace_sibling",
        ]

        for p in invalid_paths:
            step = {"action_type": "SEARCH_FILES", "content": "*.txt", "path": p}
            task = runtime.create_task(f"Spy search {p}", [step])
            res = runtime.execute_next_step(task.id)
            assert res is not None
            assert res["success"] is False
            assert res["error"] == "PathBoundaryViolation"

        # Assert ZERO subprocess.run and ZERO subprocess.Popen invocations occurred
        assert len(intercepted_runs) == 0
        assert len(intercepted_popens) == 0

    # ──────────────────────────────────────────────────────────────────────────
    # Normal In-Workspace and Persistence Functionality
    # ──────────────────────────────────────────────────────────────────────────

    def test_normal_in_workspace_operations_succeed(
        self, probe_fixture: dict[str, Any]
    ) -> None:
        """Normal relative file operations strictly inside workspace succeed."""
        runtime = TaskRuntime()

        # 1. WRITE_FILE
        write_step = {
            "action_type": "WRITE_FILE",
            "path": "subfolder/hello.txt",
            "content": "HELLO_SAFE_WORKSPACE_WAVE5",
        }
        task1 = runtime.create_task("Safe write", [write_step])
        res1 = runtime.execute_next_step(task1.id)
        assert res1 is not None and res1["success"] is True

        target = probe_fixture["workspace"] / "subfolder" / "hello.txt"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "HELLO_SAFE_WORKSPACE_WAVE5"

        # 2. READ_FILE
        read_step = {
            "action_type": "READ_FILE",
            "path": "subfolder/hello.txt",
        }
        task2 = runtime.create_task("Safe read", [read_step])
        res2 = runtime.execute_next_step(task2.id)
        assert res2 is not None and res2["success"] is True
        assert "HELLO_SAFE_WORKSPACE_WAVE5" in res2["message"]

        # 3. SEND_FILE
        send_step = {
            "action_type": "SEND_FILE",
            "path": "subfolder/hello.txt",
        }
        task3 = runtime.create_task("Safe send", [send_step])
        res3 = runtime.execute_next_step(task3.id)
        assert res3 is not None and res3["success"] is True
        assert "готов к отправке" in res3["message"]

        # 4. SEARCH_FILES
        search_step = {
            "action_type": "SEARCH_FILES",
            "content": "hello.txt",
            "path": "subfolder",
        }
        task4 = runtime.create_task("Safe search", [search_step])
        res4 = runtime.execute_next_step(task4.id)
        assert res4 is not None and res4["success"] is True
        assert "hello.txt" in res4["message"]

    def test_normal_persistence_lifecycle(
        self, probe_fixture: dict[str, Any]
    ) -> None:
        """Normal persistence operations with valid ASCII IDs succeed."""
        task = Task(
            id="task-valid-wave5-1234",
            goal="Normal Persistence Task",
            steps=[{"action_type": "WRITE_FILE", "path": "test.txt", "content": "ok"}],
            status="pending",
            created_at=2000.0,
        )
        _save_task(task)

        loaded = _load_task("task-valid-wave5-1234")
        assert loaded is not None
        assert loaded.id == "task-valid-wave5-1234"
        assert loaded.goal == "Normal Persistence Task"

        all_tasks = _list_tasks()
        assert any(t.id == "task-valid-wave5-1234" for t in all_tasks)

        _delete_task("task-valid-wave5-1234")
        assert _load_task("task-valid-wave5-1234") is None

    # ──────────────────────────────────────────────────────────────────────────
    # Workspace Override and Default Boundaries
    # ──────────────────────────────────────────────────────────────────────────

    def test_workspace_override_rejects_slash_root_and_root(self) -> None:
        """Reject workspace override at '/' or the real home directory before mkdir."""
        from antigona.core import paths

        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace="/")
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=str(paths.home_dir()))

    def test_workspace_override_rejects_repo_and_subdirectories(self) -> None:
        """Reject workspace override at project_root or inside project_root."""
        from antigona.core import paths

        repo_root = paths.project_root().resolve()
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=repo_root)
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=repo_root / "src")
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=repo_root / "workspace")

    def test_workspace_override_rejects_directories_with_git_or_venv(
        self, tmp_path: Path
    ) -> None:
        """Reject workspace override pointing to directories containing .git or .venv."""
        git_dir = tmp_path / "custom_ws_git"
        (git_dir / ".git").mkdir(parents=True, exist_ok=True)
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=git_dir)

        venv_dir = tmp_path / "custom_ws_venv"
        (venv_dir / ".venv").mkdir(parents=True, exist_ok=True)
        with pytest.raises(PathBoundaryViolation):
            TaskRuntime(workspace=venv_dir)

    def test_production_default_unmodified_env_external_root_and_no_io(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Production default under unmodified environment proves external root and no IO on .git/.venv."""
        # Ensure clean unmodified environment for task workspace
        monkeypatch.delenv("ANTIGONA_TASK_WORKSPACE", raising=False)
        monkeypatch.delenv("ANTIGONA_TASK_ACTION_WORKSPACE", raising=False)
        monkeypatch.delenv("ANTIGONA_STATE_ROOT", raising=False)
        monkeypatch.delenv("ANTIGONA_WORKSPACE", raising=False)

        import shutil

        # The external task workspace belongs to the operator: this audit proves the
        # fence and must never delete real state.  A foreign .git there makes the
        # audit inconclusive, so fail loudly; only this test's own probe residue
        # (.git/PROBE_ATTACK.txt, written below) may be removed.
        external_git = Path("/var/lib/antigona/workspace/tasks/.git")
        if external_git.exists():
            residue = sorted(entry.name for entry in external_git.iterdir())
            assert residue == ["PROBE_ATTACK.txt"], (
                "refusing to run: unexpected content in the production task workspace "
                f"{external_git}: {residue}; this test must not delete operator data"
            )
            shutil.rmtree(external_git)

        runtime = TaskRuntime()
        expected_default = Path("/var/lib/antigona/workspace/tasks")
        assert runtime.workspace == expected_default

        # Prove external root is NOT source / project root and not inside repo
        from antigona.core import paths

        repo_root = paths.project_root().resolve()
        assert runtime.workspace != repo_root
        assert repo_root not in runtime.workspace.parents
        assert runtime.workspace not in repo_root.parents

        # Prove relative inputs targeting .git or .venv deny / have no IO on source repo
        git_head = repo_root / ".git" / "HEAD"
        git_head_original = (
            git_head.read_text(encoding="utf-8") if git_head.exists() else None
        )

        # 1. READ_FILE .git relative input
        read_step = {"action_type": "READ_FILE", "path": ".git/HEAD"}
        t1 = runtime.create_task("Read git", [read_step])
        res1 = runtime.execute_next_step(t1.id)
        assert res1 is not None and res1["success"] is False
        if git_head_original:
            assert git_head_original not in str(res1.get("message", ""))

        # 2. WRITE_FILE .git relative input - no mutation on repo
        write_step = {
            "action_type": "WRITE_FILE",
            "path": ".git/PROBE_ATTACK.txt",
            "content": "ATTACK",
        }
        t2 = runtime.create_task("Write git", [write_step])
        res2 = runtime.execute_next_step(t2.id)
        assert res2 is not None
        repo_attack_file = repo_root / ".git" / "PROBE_ATTACK.txt"
        assert not repo_attack_file.exists()

        # 3. Traversal .git relative input
        trav_step = {
            "action_type": "WRITE_FILE",
            "path": "../.git/PROBE_TRAV.txt",
            "content": "TRAV",
        }
        t3 = runtime.create_task("Write trav git", [trav_step])
        res3 = runtime.execute_next_step(t3.id)
        assert res3 is not None and res3["success"] is False
        assert res3["error"] == "PathBoundaryViolation"
        repo_trav_file = repo_root / ".git" / "PROBE_TRAV.txt"
        assert not repo_trav_file.exists()

        # 4. READ_FILE .venv relative input
        venv_step = {"action_type": "READ_FILE", "path": ".venv/pyvenv.cfg"}
        t4 = runtime.create_task("Read venv", [venv_step])
        res4 = runtime.execute_next_step(t4.id)
        assert res4 is not None and res4["success"] is False

        # 5. SEARCH_FILES .git relative input
        search_step = {
            "action_type": "SEARCH_FILES",
            "content": "HEAD",
            "path": ".git",
        }
        t5 = runtime.create_task("Search git", [search_step])
        res5 = runtime.execute_next_step(t5.id)
        # Search target is /var/lib/antigona/workspace/tasks/.git which doesn't exist, zero repo files found
        if res5 is not None and res5["success"]:
            assert "0" in res5["message"] or "ничего не найдено" in res5["message"]

        # The probe above writes inside its OWN sandbox workspace, so ``.git`` is a
        # plain relative directory there and the write is expected to succeed.
        # Remove exactly that residue and fail if anything else appeared: the audit
        # must leave the operator's workspace as it found it.
        external_git = runtime.workspace / ".git"
        if external_git.exists():
            residue = sorted(entry.name for entry in external_git.iterdir())
            assert residue == ["PROBE_ATTACK.txt"], residue
            shutil.rmtree(external_git)

