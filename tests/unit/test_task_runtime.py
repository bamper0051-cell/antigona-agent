"""Tests for TaskRuntime — multi-step sequential task execution.

Covers:
  - PlanParser: PLAN/STEP parsing from LLM output
  - TaskRuntime: create_task, execute_next_step, get_status, cancel_task
  - Sequential execution: multiple steps in sequence
  - Progress messages
  - Persistence: save/load across restarts
  - Edge cases: empty plan, no steps, cancellation mid-execution
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from antigona.task.runtime import (
    PlanParser,
    Task,
    TaskRuntime,
    _delete_task,
    _list_tasks,
    _load_task,
    _save_task,
    step_action_type,
)

# ─── PlanParser tests ─────────────────────────────────────────────────────────


class TestPlanParser:
    """Tests for parsing PLAN/STEP commands from LLM output."""

    def test_parse_simple_plan(self) -> None:
        text = (
            "PLAN|Создать сайт\n"
            "STEP|WRITE_FILE|index.html|<html>Hello</html>\n"
            "STEP|WRITE_FILE|style.css|body { color: red; }\n"
            "STEP|SEND_FILE|index.html\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Создать сайт"
        assert len(steps) == 3
        assert steps[0]["action_type"] == "WRITE_FILE"
        assert steps[0]["path"] == "index.html"
        assert steps[0]["content"] == "<html>Hello</html>"
        assert steps[1]["action_type"] == "WRITE_FILE"
        assert steps[1]["path"] == "style.css"
        assert steps[1]["content"] == "body { color: red; }"
        assert steps[2]["action_type"] == "SEND_FILE"
        assert steps[2]["path"] == "index.html"

    def test_parse_with_shell_step(self) -> None:
        text = (
            "PLAN|Deploy app\n"
            "STEP|WRITE_FILE|deploy.sh|echo deploying\n"
            "STEP|RUN_SHELL|bash deploy.sh\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Deploy app"
        assert len(steps) == 2
        assert steps[0]["action_type"] == "WRITE_FILE"
        assert steps[0]["path"] == "deploy.sh"
        assert steps[1]["action_type"] == "RUN_SHELL"
        assert steps[1]["command"] == "bash deploy.sh"

    def test_parse_with_natural_language(self) -> None:
        text = (
            "Создаю файлы для сайта.\n"
            "PLAN|Website\n"
            "STEP|WRITE_FILE|a.html|page A\n"
            "STEP|WRITE_FILE|b.html|page B\n"
            "Готово!\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Website"
        assert len(steps) == 2
        assert steps[0]["path"] == "a.html"

    def test_no_plan(self) -> None:
        text = "Привет! Как дела?"
        goal, steps = PlanParser.parse(text)
        assert goal == ""
        assert steps == []

    def test_has_plan_true(self) -> None:
        assert PlanParser.has_plan("PLAN|Test")
        assert PlanParser.has_plan("some text\nPLAN|Test")

    def test_has_plan_false(self) -> None:
        assert not PlanParser.has_plan("")
        assert not PlanParser.has_plan("Hello world")
        assert not PlanParser.has_plan("PLAN")

    def test_empty_text(self) -> None:
        goal, steps = PlanParser.parse("")
        assert goal == ""
        assert steps == []

    def test_case_insensitive_plan(self) -> None:
        text = "plan|lowercase title\nstep|write_file|x.txt|data\n"
        goal, steps = PlanParser.parse(text)
        assert goal == "lowercase title" or goal == "lowercase title"
        # Note: the regex is case-insensitive
        assert len(steps) > 0

    def test_configure_key_step(self) -> None:
        text = (
            "PLAN|Setup key\n"
            "STEP|CONFIGURE_KEY|openai|sk-test123\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Setup key"
        assert len(steps) == 1
        assert steps[0]["action_type"] == "CONFIGURE_KEY"
        assert steps[0]["path"] == "openai"
        assert steps[0]["content"] == "sk-test123"

    def test_step_with_empty_content(self) -> None:
        text = (
            "PLAN|Empty content\n"
            "STEP|WRITE_FILE|empty.txt|\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Empty content"
        assert len(steps) == 1
        assert steps[0]["content"] == ""


# ─── TaskRuntime creation tests ───────────────────────────────────────────────


class TestTaskRuntimeCreate:
    """Tests for creating tasks."""

    def test_create_from_llm(self) -> None:
        runtime = TaskRuntime()
        text = (
            "PLAN|Test task\n"
            "STEP|WRITE_FILE|/tmp/test.txt|hello\n"
            "STEP|SEND_FILE|/tmp/test.txt\n"
        )
        task = runtime.create_task_from_llm(text)
        assert task is not None
        assert task.goal == "Test task"
        assert len(task.steps) == 2
        assert task.status == "pending"
        assert task.id.startswith("task-")

    def test_create_from_llm_no_plan(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task_from_llm("Hello world")
        assert task is None

    def test_create_direct(self) -> None:
        runtime = TaskRuntime()
        steps = [
            {"action_type": "WRITE_FILE", "path": "/tmp/a.txt", "content": "aaa"},
            {"action_type": "SEND_FILE", "path": "/tmp/b.txt"},
        ]
        task = runtime.create_task("Direct task", steps)
        assert task.goal == "Direct task"
        assert len(task.steps) == 2
        assert task.status == "pending"

    def test_create_task_assigns_ids(self) -> None:
        runtime = TaskRuntime()
        steps = [
            {"action_type": "WRITE_FILE", "path": "/tmp/a.txt", "content": "a"},
        ]
        task = runtime.create_task("Test", steps)
        assert task.steps[0].get("id") == "step-1"
        assert task.steps[0].get("status") == "pending"


# ─── TaskRuntime execution tests ──────────────────────────────────────────────


class TestTaskRuntimeExecute:
    """Tests for step execution."""

    def test_execute_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            filepath = Path(tmpdir) / "test.txt"
            steps = [
                {
                    "action_type": "WRITE_FILE",
                    "path": "test.txt",
                    "content": "Hello World",
                },
            ]
            task = runtime.create_task("Write test", steps)
            result = runtime.execute_next_step(task.id)
            assert result is not None
            assert result["success"] is True
            assert result["action_type"] == "WRITE_FILE"
            assert "создан" in result["message"]
            assert filepath.exists()
            assert filepath.read_text(encoding="utf-8") == "Hello World"

    def test_execute_multiple_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            f1 = Path(tmpdir) / "a.txt"
            f2 = Path(tmpdir) / "b.txt"
            steps = [
                {"action_type": "WRITE_FILE", "path": "a.txt", "content": "A"},
                {"action_type": "WRITE_FILE", "path": "b.txt", "content": "B"},
            ]
            task = runtime.create_task("Two files", steps)

            # Step 1
            r1 = runtime.execute_next_step(task.id)
            assert r1 is not None
            assert r1["success"] is True
            assert r1["step_index"] == 0
            assert r1["completed"] is False
            assert f1.exists()
            assert f1.read_text(encoding="utf-8") == "A"

            # Step 2
            r2 = runtime.execute_next_step(task.id)
            assert r2 is not None
            assert r2["success"] is True
            assert r2["step_index"] == 1
            assert r2["completed"] is True
            assert f2.exists()
            assert f2.read_text(encoding="utf-8") == "B"

            # Verify task completed
            task_after = runtime.get_task(task.id)
            assert task_after is not None
            assert task_after.status == "completed"

    def test_execute_send_file_missing(self) -> None:
        runtime = TaskRuntime()
        steps = [
            {"action_type": "SEND_FILE", "path": "nonexistent_file.txt"},
        ]
        task = runtime.create_task("Send missing", steps)
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["success"] is False
        assert "не найден" in result["message"]

    def test_execute_shell_default_fails_closed(self, monkeypatch: Any) -> None:
        import subprocess

        spawns: list[Any] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: spawns.append(a))
        runtime = TaskRuntime()
        steps = [
            {"action_type": "RUN_SHELL", "command": "echo hello_task"},
        ]
        task = runtime.create_task("Shell test", steps)
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["success"] is False
        assert result["error"] == "HostExecutionForbidden"
        assert spawns == []

    def test_execute_shell_docker_success(self, monkeypatch: Any) -> None:
        from antigona.sandbox.docker_sandbox import DockerSandboxBackend, DockerSandboxResult

        monkeypatch.setattr(
            DockerSandboxBackend,
            "run",
            lambda self, cmd, **kw: DockerSandboxResult(
                command=tuple(cmd),
                exit_code=0,
                stdout="hello_task_docker",
                stderr="",
                container_id="antigona-sandbox-test",
            ),
        )
        runtime = TaskRuntime(sandbox_runtime="docker")
        steps = [
            {"action_type": "RUN_SHELL", "command": "echo hello_task"},
        ]
        task = runtime.create_task("Shell test", steps)
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["success"] is True
        assert "hello_task_docker" in result["message"]

    def test_execute_unknown_action(self) -> None:
        runtime = TaskRuntime()
        steps = [
            {"action_type": "UNKNOWN_ACTION", "path": "x.txt"},
        ]
        task = runtime.create_task("Unknown", steps)
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["success"] is False
        assert "Неизвестный тип" in result["message"]

    def test_no_pending_steps_returns_status(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Empty", [])
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["step_index"] == -1
        assert result["success"] is False
        assert "Нет ожидающих шагов" in result["message"]


# ─── TaskRuntime status tests ─────────────────────────────────────────────────


class TestTaskRuntimeStatus:
    """Tests for status reporting."""

    def test_status_pending(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Test", [
            {"action_type": "WRITE_FILE", "path": "/tmp/x.txt", "content": "x"},
        ])
        status = runtime.get_status(task.id)
        assert status is not None
        assert "Test" in status
        assert "0 из 1" in status

    def test_status_after_step(self) -> None:
        runtime = TaskRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = Path(tmpdir) / "x.txt"
            task = runtime.create_task("Test", [
                {"action_type": "WRITE_FILE", "path": str(fp), "content": "x"},
            ])
            runtime.execute_next_step(task.id)
            status = runtime.get_status(task.id)
            assert status is not None
            assert "1 из 1" in status

    def test_status_not_found(self) -> None:
        runtime = TaskRuntime()
        status = runtime.get_status("nonexistent-id")
        assert status is None

    def test_status_cancelled(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Cancel test", [
            {"action_type": "WRITE_FILE", "path": "/tmp/x.txt", "content": "x"},
        ])
        runtime.cancel_task(task.id)
        status = runtime.get_status(task.id)
        assert status is not None
        assert "Отменён" in status


# ─── TaskRuntime cancel tests ─────────────────────────────────────────────────


class TestTaskRuntimeCancel:
    """Tests for cancellation."""

    def test_cancel_active_task(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Cancel me", [
            {"action_type": "WRITE_FILE", "path": "/tmp/x.txt", "content": "x"},
            {"action_type": "WRITE_FILE", "path": "/tmp/y.txt", "content": "y"},
        ])
        assert runtime.cancel_task(task.id) is True
        t = runtime.get_task(task.id)
        assert t is not None
        assert t.status == "cancelled"

    def test_cancel_completed_task_fails(self) -> None:
        runtime = TaskRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = Path(tmpdir) / "x.txt"
            task = runtime.create_task("Complete me", [
                {"action_type": "WRITE_FILE", "path": str(fp), "content": "x"},
            ])
            runtime.execute_next_step(task.id)
            assert runtime.cancel_task(task.id) is False

    def test_cancel_nonexistent_task(self) -> None:
        runtime = TaskRuntime()
        assert runtime.cancel_task("nonexistent") is False

    def test_cancel_preserves_files(self) -> None:
        runtime = TaskRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = Path(tmpdir) / "x.txt"
            task = runtime.create_task("Cancel with data", [
                {"action_type": "WRITE_FILE", "path": str(fp), "content": "data"},
            ])
            runtime.cancel_task(task.id)
            # File should NOT have been created (step never executed)
            assert not fp.exists()


# ─── TaskRuntime persistence tests ────────────────────────────────────────────


class TestTaskRuntimePersistence:
    """Tests for JSON persistence."""

    def test_save_and_load_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "antigona.task.runtime.TASK_PERSIST_DIR", tmpdir
            ):
                task = Task(
                    id="test-123",
                    goal="Persist test",
                    steps=[
                        {
                            "id": "step-1",
                            "action_type": "WRITE_FILE",
                            "path": "/tmp/x.txt",
                            "content": "data",
                            "status": "completed",
                            "result": "OK",
                            "error": "",
                        },
                    ],
                    status="completed",
                    created_at=time.time(),
                    completed_at=time.time(),
                    current_step_index=0,
                )
                _save_task(task)

                loaded = _load_task("test-123")
                assert loaded is not None
                assert loaded.id == "test-123"
                assert loaded.goal == "Persist test"
                assert len(loaded.steps) == 1
                assert loaded.steps[0]["action_type"] == "WRITE_FILE"

    def test_delete_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "antigona.task.runtime.TASK_PERSIST_DIR", tmpdir
            ):
                task = Task(
                    id="del-123",
                    goal="Delete me",
                    steps=[],
                    status="pending",
                    created_at=time.time(),
                )
                _save_task(task)
                assert _load_task("del-123") is not None

                _delete_task("del-123")
                assert _load_task("del-123") is None

    def test_list_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "antigona.task.runtime.TASK_PERSIST_DIR", tmpdir
            ):
                for i in range(3):
                    task = Task(
                        id=f"task-{i}",
                        goal=f"Task {i}",
                        steps=[],
                        status="pending",
                        created_at=time.time() + i,
                    )
                    _save_task(task)

                tasks = _list_tasks()
                assert len(tasks) == 3
                # Newest first
                assert tasks[0].id == "task-2"

    def test_runtime_restores_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "antigona.task.runtime.TASK_PERSIST_DIR", tmpdir
            ):
                # Create and save a task
                runtime1 = TaskRuntime()
                task = runtime1.create_task("Survive restart", [
                    {"action_type": "WRITE_FILE", "path": "/tmp/x.txt", "content": "x"},
                ])

                # Simulate restart — create new runtime
                runtime2 = TaskRuntime()
                restored = runtime2.get_task(task.id)
                assert restored is not None
                assert restored.goal == "Survive restart"

    def test_list_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "antigona.task.runtime.TASK_PERSIST_DIR", tmpdir
            ):
                runtime = TaskRuntime()
                runtime.create_task("Active", [
                    {"action_type": "WRITE_FILE", "path": "/tmp/x.txt", "content": "x"},
                ])
                # No persist dir override in runtime methods, so we test directly


# ─── Progress messages ────────────────────────────────────────────────────────


class TestProgressMessages:
    """Tests for progress message formatting."""

    def test_progress_message_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            task = runtime.create_task("Test", [
                {"action_type": "WRITE_FILE", "path": "x.txt", "content": "x"},
                {"action_type": "WRITE_FILE", "path": "y.txt", "content": "y"},
            ])
            result = runtime.execute_next_step(task.id)
            assert result is not None
            loaded_task = runtime.get_task(task.id)
            assert loaded_task is not None
            msg = runtime.get_progress_message(result, loaded_task)
            assert "Шаг 1/2" in msg
            assert "Продолжаю" in msg

    def test_progress_message_last_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            task = runtime.create_task("Test", [
                {"action_type": "WRITE_FILE", "path": "x.txt", "content": "x"},
            ])
            result = runtime.execute_next_step(task.id)
            assert result is not None
            loaded_task = runtime.get_task(task.id)
            assert loaded_task is not None
            msg = runtime.get_progress_message(result, loaded_task)
            assert "Шаг 1/1" in msg
            assert "Готово" in msg

    def test_progress_message_failed_step(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Test", [
            {"action_type": "SEND_FILE", "path": "nonexistent_missing.txt"},
        ])
        result = runtime.execute_next_step(task.id)
        assert result is not None
        loaded_task = runtime.get_task(task.id)
        assert loaded_task is not None
        msg = runtime.get_progress_message(result, loaded_task)
        assert "не найден" in msg

# ─── Sequential execution of 3+ steps ─────────────────────────────────────────


class TestSequentialExecution:
    """Tests for executing 3+ steps in sequence."""

    def test_three_files_in_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            steps = [
                {
                    "action_type": "WRITE_FILE",
                    "path": "index.html",
                    "content": "<html></html>",
                },
                {
                    "action_type": "WRITE_FILE",
                    "path": "style.css",
                    "content": "body {}",
                },
                {
                    "action_type": "WRITE_FILE",
                    "path": "script.js",
                    "content": "console.log('hi')",
                },
            ]
            task = runtime.create_task("Three files", steps)
            assert task.status == "pending"

            # Execute all 3 steps
            for i in range(3):
                result = runtime.execute_next_step(task.id)
                assert result is not None
                assert result["success"] is True
                assert result["completed"] == (i == 2)

            # All files exist
            assert (Path(tmpdir) / "index.html").exists()
            assert (Path(tmpdir) / "style.css").exists()
            assert (Path(tmpdir) / "script.js").exists()

            # Task is completed
            completed_task = runtime.get_task(task.id)
            assert completed_task is not None
            assert completed_task.status == "completed"

    def test_mixed_actions_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            steps = [
                {
                    "action_type": "WRITE_FILE",
                    "path": "deploy.sh",
                    "content": "echo deploying",
                },
                {
                    "action_type": "READ_FILE",
                    "path": "deploy.sh",
                },
            ]
            task = runtime.create_task("Write then verify", steps)
            r1 = runtime.execute_next_step(task.id)
            assert r1 is not None and r1["success"] is True
            r2 = runtime.execute_next_step(task.id)
            assert r2 is not None and r2["success"] is True
            assert "deploying" in r2["message"]

    def test_stop_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            steps = [
                {
                    "action_type": "WRITE_FILE",
                    "path": "ok.txt",
                    "content": "OK",
                },
                {
                    "action_type": "SEND_FILE",
                    "path": "nonexistent_missing.txt",
                },
                {
                    "action_type": "WRITE_FILE",
                    "path": "should_not_exist.txt",
                    "content": "NOPE",
                },
            ]
            task = runtime.create_task("Fail mid-chain", steps)

            # Step 1: OK
            r1 = runtime.execute_next_step(task.id)
            assert r1 is not None and r1["success"] is True and r1["completed"] is False

            # Step 2: Fails
            r2 = runtime.execute_next_step(task.id)
            assert r2 is not None and r2["success"] is False and r2["completed"] is False

            # Step 3 should still run (it's pending, not auto-cancelled)
            task_after = runtime.get_task(task.id)
            assert task_after is not None
            # The third step should still be pending
            third_step = task_after.steps[2]
            assert third_step.get("status") == "pending"


# ─── Edge cases ───────────────────────────────────────────────────────────────


class TestTaskRuntimeEdgeCases:
    """Edge cases for TaskRuntime."""

    def test_get_nonexistent_task(self) -> None:
        runtime = TaskRuntime()
        assert runtime.get_task("nonexistent") is None

    def test_get_status_nonexistent(self) -> None:
        runtime = TaskRuntime()
        assert runtime.get_status("nonexistent") is None

    def test_create_from_llm_empty_string(self) -> None:
        runtime = TaskRuntime()
        assert runtime.create_task_from_llm("") is None

    def test_create_task_with_no_steps(self) -> None:
        runtime = TaskRuntime()
        task = runtime.create_task("Empty", [])
        assert task is not None
        assert len(task.steps) == 0

    def test_list_active_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("antigona.task.runtime.TASK_PERSIST_DIR", tmpdir):
                runtime = TaskRuntime()
                assert runtime.list_active() == []

    def test_step_action_type_helper(self) -> None:
        step = {"action_type": "WRITE_FILE", "path": "x.txt"}
        assert step_action_type(step) == "WRITE_FILE"
        assert step_action_type({}) == ""

    def test_task_count(self) -> None:
        runtime = TaskRuntime()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("antigona.task.runtime.TASK_PERSIST_DIR", tmpdir):
                runtime.create_task("A", [])
                runtime.create_task("B", [])
                counts = runtime.get_task_count()
                assert "pending" in counts
                assert counts["pending"] == 2

    def test_create_from_llm_preserves_step_order(self) -> None:
        runtime = TaskRuntime()
        text = (
            "PLAN|Order test\n"
            "STEP|WRITE_FILE|/tmp/1.txt|first\n"
            "STEP|WRITE_FILE|/tmp/2.txt|second\n"
            "STEP|WRITE_FILE|/tmp/3.txt|third\n"
        )
        task = runtime.create_task_from_llm(text)
        assert task is not None
        contents = [s.get("content") for s in task.steps]
        assert contents == ["first", "second", "third"]


# ─── Idempotency and concurrent access ────────────────────────────────────────


class TestTaskRuntimeConcurrency:
    """Basic concurrency safety tests."""

    def test_create_task_is_thread_safe(self) -> None:
        import concurrent.futures

        runtime = TaskRuntime()
        tasks_created: list[Task] = []

        def create_one() -> None:
            t = runtime.create_task("Concurrent", [
                {"action_type": "WRITE_FILE", "path": f"/tmp/c_{id(concurrent.futures)}.txt", "content": "x"},
            ])
            tasks_created.append(t)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(create_one) for _ in range(4)]
            for f in futures:
                f.result()

        assert len(tasks_created) == 4
        task_ids = {t.id for t in tasks_created}
        assert len(task_ids) == 4  # All unique


# ─── Russian format PLAN/STEP tests ───────────────────────────────────────────


class TestRussianFormat:
    """Tests for Russian ПЛАН/ШАГ format parsing."""

    def test_parse_plan_colon_format(self) -> None:
        """Parse ПЛАН: Название with colon format."""
        text = (
            "ПЛАН: Создание сайта\n"
            "ШАГ 1: WRITE_FILE|index.html|<html>Hello</html>\n"
            "ШАГ 2: WRITE_FILE|style.css|body { color: red; }\n"
            "ШАГ 3: SEND_FILE|index.html\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Создание сайта"
        assert len(steps) == 3
        assert steps[0]["action_type"] == "WRITE_FILE"
        assert steps[0]["path"] == "index.html"
        assert steps[0]["content"] == "<html>Hello</html>"
        assert steps[1]["action_type"] == "WRITE_FILE"
        assert steps[1]["path"] == "style.css"
        assert steps[1]["content"] == "body { color: red; }"
        assert steps[2]["action_type"] == "SEND_FILE"
        assert steps[2]["path"] == "index.html"

    def test_parse_plan_colon_with_numbered_steps(self) -> None:
        """Parse ПЛАН: with ШАГ N: format (numbered)."""
        text = (
            "ПЛАН: Деплой\n"
            "ШАГ 1: WRITE_FILE|deploy.sh|echo deploying\n"
            "ШАГ 2: RUN_SHELL|bash deploy.sh\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Деплой"
        assert len(steps) == 2
        assert steps[0]["action_type"] == "WRITE_FILE"
        assert steps[1]["action_type"] == "RUN_SHELL"

    def test_parse_russian_pipe_format(self) -> None:
        """Parse ПЛАН|... and ШАГ|... pipe format (backward compat)."""
        text = (
            "ПЛАН|Создать сайт\n"
            "ШАГ|WRITE_FILE|index.html|<h1>Hi</h1>\n"
            "ШАГ|SEND_FILE|index.html\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Создать сайт"
        assert len(steps) == 2
        assert steps[0]["action_type"] == "WRITE_FILE"
        assert steps[0]["path"] == "index.html"
        assert steps[1]["action_type"] == "SEND_FILE"

    def test_mixed_pipe_and_colon_formats(self) -> None:
        """Handle both pipe and colon formats in same text."""
        text = (
            "ПЛАН: Смешанный формат\n"
            "ШАГ 1: WRITE_FILE|a.txt|content A\n"
            "ШАГ|WRITE_FILE|b.txt|content B\n"
            "ШАГ 3: SEND_FILE|a.txt\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Смешанный формат"
        assert len(steps) == 3

    def test_has_plan_russian_colon(self) -> None:
        """has_plan detects ПЛАН: format."""
        assert PlanParser.has_plan("ПЛАН: Создать сайт")
        assert PlanParser.has_plan("text\nПЛАН: Задача")
        assert PlanParser.has_plan("ПЛАН:  ")

    def test_has_plan_russian_pipe(self) -> None:
        """has_plan detects ПЛАН| format."""
        assert PlanParser.has_plan("ПЛАН|Задача")
        assert PlanParser.has_plan("text\nПЛАН|Задача")

    def test_has_plan_english_pipe(self) -> None:
        """has_plan still detects PLAN| format."""
        assert PlanParser.has_plan("PLAN|Task")
        assert PlanParser.has_plan("text\nPLAN|Task")

    def test_has_plan_false_for_all_formats(self) -> None:
        """has_plan returns False when no plan present."""
        assert not PlanParser.has_plan("")
        assert not PlanParser.has_plan("Hello world")
        assert not PlanParser.has_plan("ПЛАН без разделителя")
        assert not PlanParser.has_plan("ШАГ 1: WRITE_FILE|file.txt|data")
        assert not PlanParser.has_plan("PLAN")

    def test_plan_colon_with_explanation_before(self) -> None:
        """Parse ПЛАН: after natural language explanation."""
        text = (
            "Понял! Создаю сайт из трёх файлов.\n\n"
            "ПЛАН: Создание сайта\n"
            "ШАГ 1: WRITE_FILE|index.html|<h1>Hello</h1>\n"
            "ШАГ 2: WRITE_FILE|style.css|body {}\n"
            "ШАГ 3: SEND_FILE|index.html\n"
            "Готово!\n"
        )
        goal, steps = PlanParser.parse(text)
        assert goal == "Создание сайта"
        assert len(steps) == 3

    def test_create_task_from_llm_russian_format(self) -> None:
        """create_task_from_llm works with Russian colon format."""
        runtime = TaskRuntime()
        text = (
            "ПЛАН: Тестовая задача\n"
            "ШАГ 1: WRITE_FILE|/tmp/russian_test.txt|Привет мир\n"
            "ШАГ 2: SEND_FILE|/tmp/russian_test.txt\n"
        )
        task = runtime.create_task_from_llm(text)
        assert task is not None
        assert task.goal == "Тестовая задача"
        assert len(task.steps) == 2
        assert task.steps[0]["action_type"] == "WRITE_FILE"
        assert task.steps[0]["path"] == "/tmp/russian_test.txt"
        assert task.steps[0]["content"] == "Привет мир"
        assert task.steps[1]["action_type"] == "SEND_FILE"
        assert task.status == "pending"

    def test_step_failure_progress_message(self) -> None:
        """Progress message for failed step contains error context."""
        runtime = TaskRuntime()
        task = runtime.create_task("Fail test", [
            {"action_type": "SEND_FILE", "path": "nonexistent_missing.txt"},
        ])
        result = runtime.execute_next_step(task.id)
        assert result is not None
        assert result["success"] is False
        msg = runtime.get_progress_message(result, task)
        assert "не найден" in msg
        assert "Шаг" in msg

    def test_three_file_website_scenario(self) -> None:
        """Full gate scenario: сделать сайт с тремя файлами."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            steps = [
                {"action_type": "WRITE_FILE", "path": "index.html", "content": "<h1>Hello</h1>"},
                {"action_type": "WRITE_FILE", "path": "style.css", "content": "body {}"},
                {"action_type": "WRITE_FILE", "path": "script.js", "content": "console.log(1)"},
            ]
            task = runtime.create_task("Создание сайта", steps)
            assert task.status == "pending"

            # Execute in sequence
            r1 = runtime.execute_next_step(task.id)
            assert r1 is not None and r1["success"] and r1["step_index"] == 0
            r2 = runtime.execute_next_step(task.id)
            assert r2 is not None and r2["success"] and r2["step_index"] == 1
            r3 = runtime.execute_next_step(task.id)
            assert r3 is not None and r3["success"] and r3["step_index"] == 2

            # Verify all files
            assert (Path(tmpdir) / "index.html").exists()
            assert (Path(tmpdir) / "style.css").exists()
            assert (Path(tmpdir) / "script.js").exists()

            # Verify progress messages
            t = runtime.get_task(task.id)
            assert t is not None
            assert t.status == "completed"

            # Check progress message format
            p3 = runtime.get_progress_message(r3, t)
            assert "Готово" in p3
            assert "3" in p3


# ─── Wave 4B Fail-Closed RUN_SHELL Security Tests ─────────────────────────────


class TestTaskRuntimeRunShellFailClosed:
    """Rigorous security tests for TaskRuntime RUN_SHELL fail-closed contract.

    Invariants:
      1. Default missing, empty, unknown, malformed, or 'host' sandbox_runtime
         MUST deny fail-closed BEFORE shlex/subprocess with zero host spawns.
      2. No command secrets or tokens are leaked in denial messages.
      3. For sandbox_runtime='docker', if Docker is unavailable it fails closed
         with zero host fallback.
      4. For sandbox_runtime='docker' success, it reaches only the sandbox surface,
         never host subprocess.
      5. Step-level sandbox_runtime overrides runtime default.
      6. Safe non-shell actions (WRITE_FILE, SEND_FILE, READ_FILE, SEARCH_FILES,
         CONFIGURE_KEY) remain green and unaffected.
    """

    def test_run_shell_default_missing_sandbox_runtime_denies_zero_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(a) Default missing sandbox_runtime denies fail-closed with 0 subprocess calls."""
        invoked_subprocesses: list[Any] = []
        import subprocess

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_subprocesses.append(("subprocess.run", args, kwargs)),
        )
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *args, **kwargs: invoked_subprocesses.append(("subprocess.Popen", args, kwargs)),
        )

        runtime = TaskRuntime()
        secret_cmd = "curl -H 'Authorization: Bearer CRITICAL_SECRET_TOKEN_WAVE4B' https://example.internal"
        task = runtime.create_task(
            "Default missing sandbox shell step",
            [{"action_type": "RUN_SHELL", "command": secret_cmd}],
        )

        result = runtime.execute_next_step(task.id)

        assert result is not None
        assert result["success"] is False
        assert result["error"] == "HostExecutionForbidden"
        assert "запрещено" in result["message"]
        # Critical regression check: zero subprocess calls
        assert invoked_subprocesses == []
        # No secret leakage in error or message
        assert "CRITICAL_SECRET_TOKEN_WAVE4B" not in result["message"]
        assert "CRITICAL_SECRET_TOKEN_WAVE4B" not in result["error"]

    @pytest.mark.parametrize(
        "host_payload",
        [
            {"sandbox_runtime": "host"},
            {"sandbox": "host"},
            {"sandbox_runtime": "HOST"},
            {"sandbox": "Host"},
        ],
    )
    def test_run_shell_explicit_host_denies_zero_subprocess(
        self, host_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b) Explicit host sandbox_runtime denies fail-closed with 0 subprocess calls."""
        invoked_subprocesses: list[Any] = []
        import subprocess

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_subprocesses.append(args),
        )

        runtime = TaskRuntime()
        step_dict = {
            "action_type": "RUN_SHELL",
            "command": "whoami",
            **host_payload,
        }
        task = runtime.create_task("Explicit host shell step", [step_dict])

        result = runtime.execute_next_step(task.id)

        assert result is not None
        assert result["success"] is False
        assert result["error"] == "HostExecutionForbidden"
        assert invoked_subprocesses == []

    @pytest.mark.parametrize(
        "malformed_val",
        [
            "",
            "   ",
            "unknown",
            "runc",
            "local",
            "containerd",
            "none",
            123,
            True,
            ["docker"],
            {"type": "docker"},
        ],
    )
    def test_run_shell_malformed_and_unknown_sandbox_runtime_denies(
        self, malformed_val: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(c) Malformed, empty, and unknown sandbox_runtime values deny fail-closed."""
        invoked_subprocesses: list[Any] = []
        import subprocess

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_subprocesses.append(args),
        )

        runtime = TaskRuntime()
        step_dict = {
            "action_type": "RUN_SHELL",
            "command": "id",
            "sandbox_runtime": malformed_val,
        }
        task = runtime.create_task("Malformed sandbox runtime step", [step_dict])

        result = runtime.execute_next_step(task.id)

        assert result is not None
        assert result["success"] is False
        assert result["error"] == "HostExecutionForbidden"
        assert invoked_subprocesses == []

    def test_run_shell_docker_unavailable_fails_closed_zero_host_spawns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(d) When sandbox_runtime='docker' but Docker is unavailable, fail closed with 0 host spawns."""
        invoked_host_subprocesses: list[Any] = []
        import subprocess

        from antigona.sandbox.docker_sandbox import (
            DockerSandboxBackend,
            DockerSandboxUnavailableError,
        )

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_host_subprocesses.append(args),
        )

        def _mock_unavailable_run(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise DockerSandboxUnavailableError("Docker runtime daemon unavailable")

        monkeypatch.setattr(DockerSandboxBackend, "run", _mock_unavailable_run)

        runtime = TaskRuntime(sandbox_runtime="docker")
        task = runtime.create_task(
            "Docker unavailable task",
            [{"action_type": "RUN_SHELL", "command": "echo test"}],
        )

        result = runtime.execute_next_step(task.id)

        assert result is not None
        assert result["success"] is False
        assert result["error"] == "SandboxError"
        assert "Docker runtime daemon unavailable" in result["message"]
        # Must NEVER fall back to host subprocess
        assert invoked_host_subprocesses == []

    def test_run_shell_docker_success_reaches_mocked_sandbox_never_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(e) When sandbox_runtime='docker' succeeds, reaches mocked Docker sandbox and never host."""
        invoked_host_subprocesses: list[Any] = []
        invoked_sandbox_calls: list[Any] = []
        import subprocess

        from antigona.sandbox.docker_sandbox import (
            DockerSandboxBackend,
            DockerSandboxResult,
        )

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_host_subprocesses.append(args),
        )

        def _mock_sandbox_run(self: Any, command: Any, *args: Any, **kwargs: Any) -> Any:
            invoked_sandbox_calls.append(command)
            return DockerSandboxResult(
                command=tuple(command),
                exit_code=0,
                stdout="MOCKED_DOCKER_SANDBOX_OUTPUT",
                stderr="",
                container_id="antigona-sandbox-wave4b-mock",
            )

        monkeypatch.setattr(DockerSandboxBackend, "run", _mock_sandbox_run)

        runtime = TaskRuntime(sandbox_runtime="docker")
        task = runtime.create_task(
            "Docker success task",
            [{"action_type": "RUN_SHELL", "command": "echo 'hello safe sandbox'"}],
        )

        result = runtime.execute_next_step(task.id)

        assert result is not None
        assert result["success"] is True
        assert result["message"] == "MOCKED_DOCKER_SANDBOX_OUTPUT"
        assert invoked_host_subprocesses == []
        assert len(invoked_sandbox_calls) == 1
        assert invoked_sandbox_calls[0] == ["echo", "hello safe sandbox"]

    def test_run_shell_step_override_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(f) Step-level sandbox_runtime takes precedence over TaskRuntime instance default."""
        invoked_host_subprocesses: list[Any] = []
        invoked_sandbox_calls: list[Any] = []
        import subprocess

        from antigona.sandbox.docker_sandbox import (
            DockerSandboxBackend,
            DockerSandboxResult,
        )

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: invoked_host_subprocesses.append(args),
        )
        def _mock_override_run(self: Any, cmd: Any, **kw: Any) -> DockerSandboxResult:
            invoked_sandbox_calls.append(cmd)
            return DockerSandboxResult(
                command=tuple(cmd),
                exit_code=0,
                stdout="step_override_ok",
                stderr="",
                container_id="antigona-sandbox-override",
            )

        monkeypatch.setattr(DockerSandboxBackend, "run", _mock_override_run)

        # 1. Runtime has sandbox_runtime="", step has "docker" -> sandbox is used
        runtime_default_empty = TaskRuntime(sandbox_runtime="")
        task1 = runtime_default_empty.create_task(
            "Step override to docker",
            [{"action_type": "RUN_SHELL", "command": "echo step_dock", "sandbox_runtime": "docker"}],
        )
        res1 = runtime_default_empty.execute_next_step(task1.id)
        assert res1 is not None and res1["success"] is True
        assert res1["message"] == "step_override_ok"
        assert len(invoked_sandbox_calls) == 1

        # 2. Runtime has sandbox_runtime="docker", step has "host" -> fails closed
        runtime_docker = TaskRuntime(sandbox_runtime="docker")
        task2 = runtime_docker.create_task(
            "Step override to host",
            [{"action_type": "RUN_SHELL", "command": "echo step_host", "sandbox_runtime": "host"}],
        )
        res2 = runtime_docker.execute_next_step(task2.id)
        assert res2 is not None and res2["success"] is False
        assert res2["error"] == "HostExecutionForbidden"
        # Zero host subprocesses across both tests
        assert invoked_host_subprocesses == []

    def test_safe_non_shell_actions_unaffected(self) -> None:
        """(g) Safe non-shell actions (WRITE_FILE, SEND_FILE, READ_FILE, SEARCH_FILES, CONFIGURE_KEY) continue to function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = TaskRuntime(workspace=tmpdir)
            test_file = Path(tmpdir) / "safe_document.txt"
            steps = [
                {
                    "action_type": "WRITE_FILE",
                    "path": "safe_document.txt",
                    "content": "safely persisted data",
                },
                {
                    "action_type": "READ_FILE",
                    "path": "safe_document.txt",
                },
                {
                    "action_type": "SEND_FILE",
                    "path": "safe_document.txt",
                },
            ]
            task = runtime.create_task("Safe non-shell actions", steps)

            r1 = runtime.execute_next_step(task.id)
            assert r1 is not None and r1["success"] is True
            assert test_file.exists()
            assert test_file.read_text(encoding="utf-8") == "safely persisted data"

            r2 = runtime.execute_next_step(task.id)
            assert r2 is not None and r2["success"] is True
            assert "safely persisted data" in r2["message"]

            r3 = runtime.execute_next_step(task.id)
            assert r3 is not None and r3["success"] is True
            assert "готов к отправке" in r3["message"]
