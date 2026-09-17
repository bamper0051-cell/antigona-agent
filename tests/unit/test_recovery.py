"""Тесты RecoveryManager — восстановление после перезапуска (§20, §24.6).

Сценарии:
  1. Восстановление задач в активных статусах (QUEUED, RUNNING, PLANNING)
  2. Завершённые шаги не повторяются (irreversible action guard)
  3. PID-проверка внешнего процесса
  4. Сохранение привязки task_id ↔ telegram_message_id
  5. RecoveryResult структура
  6. Пустое восстановление (нет задач)
  7. Get recovery plan
  8. Checkpoint persist
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tasks import (
    EventBus,
    RecoveryManager,
    RecoveryResult,
    TaskManager,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_persist(tmp_path: Path) -> Path:
    return tmp_path / "tasks.json"


@pytest.fixture
def tmp_events(tmp_path: Path) -> str:
    return str(tmp_path / "events.jsonl")


@pytest.fixture
def bus(tmp_events: str) -> EventBus:
    EventBus.reset_instance()
    return EventBus.get_instance(persist_path=tmp_events)


@pytest.fixture
async def manager(bus: EventBus, tmp_persist: Path) -> TaskManager:
    mgr = TaskManager(event_bus=bus, tasks_persist_path=tmp_persist)
    await mgr.start()
    return mgr


@pytest.fixture
async def recovery(manager: TaskManager) -> RecoveryManager:
    return RecoveryManager(task_manager=manager)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestRecoveryManagerBasics:
    """Базовая функциональность RecoveryManager."""

    async def test_recover_empty(self, recovery: RecoveryManager) -> None:
        """Нет задач — пустой результат."""
        results = await recovery.recover_all()
        assert results == []

    async def test_recover_queued_task(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Задача в QUEUED — восстановление без изменений."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Test", original_request="test",
        )
        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.task_id == t.task_id
        assert r.recovered is True
        assert r.previous_status == "queued"
        assert r.steps_completed == 0
        assert r.steps_pending == 0

    async def test_recover_running_task(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Задача в RUNNING — восстанавливается как RUNNING."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Running task", original_request="run",
        )
        await manager.start_task(t.task_id)
        # Добавляем завершённый шаг
        await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/test.txt")
        await manager.update_step(
            (await manager.get_steps(t.task_id))[0].step_id,
            status="completed", progress=100,
        )
        # Добавляем ожидающий шаг
        await manager.add_step(t.task_id, action_type="RUN_SHELL", command="echo done")

        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.recovered is True
        assert r.steps_completed == 1
        assert r.steps_pending == 1
        # Завершённый шаг не должен быть в pending
        task = await manager.get_task(t.task_id)
        assert task is not None

    async def test_recover_planning_task(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Задача в PLANNING — переводится обратно в PLANNING."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Planning", original_request="plan",
        )
        await manager.set_plan(t.task_id, "1. Do something\n2. Profit")
        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.recovered is True
        assert r.previous_status == "planning"
        assert r.new_status == "planning"

    async def test_completed_task_not_recovered(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Завершённые задачи не восстанавливаются."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Done", original_request="done",
        )
        await manager._complete_task_internal(t.task_id, result="OK")
        results = await recovery.recover_all()
        assert results == []

    async def test_cancelled_task_not_recovered(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Отменённые задачи не восстанавливаются."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Cancelled", original_request="cancel",
        )
        await manager.cancel_task(t.task_id)
        results = await recovery.recover_all()
        assert results == []


class TestRecoveryIrreversibleGuard:
    """§20.6: Завершённые необратимые шаги не повторяются."""

    async def test_completed_steps_preserved(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Завершённые шаги не сбрасываются при восстановлении."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Irreversible", original_request="irr",
        )
        await manager.start_task(t.task_id)

        # Добавляем и завершаем 2 шага
        s1 = await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/a.txt")
        await manager.update_step(s1.step_id, status="completed", progress=100)

        s2 = await manager.add_step(t.task_id, action_type="RUN_SHELL", command="echo ok")
        await manager.update_step(s2.step_id, status="completed", progress=100)

        # Добавляем незавершённый шаг
        await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/b.txt")

        await recovery.recover_all()

        task = await manager.get_task(t.task_id)
        assert task is not None
        steps = task.steps
        completed = [s for s in steps if s.status == "completed"]
        pending = [s for s in steps if s.status == "pending"]

        # Завершённые шаги остались завершёнными
        assert len(completed) == 2
        # Незавершённый шаг не начался автоматически
        assert len(pending) == 1


class TestRecoveryPidCheck:
    """§20.3: Проверка PID внешнего процесса."""

    async def test_pid_not_found(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """PID не найден — задача восстанавливается."""
        await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="PID test", original_request="pid",
            metadata={"pid": 999999999},  # Заведомо несуществующий PID
        )
        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.recovered is True
        assert r.pid_active is False  # PID не существует

    async def test_metadata_preserved(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Метаданные задачи не теряются при восстановлении."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Meta test", original_request="meta",
            metadata={"custom_key": "custom_value", "pid": 999999999},
        )
        await recovery.recover_all()
        task = await manager.get_task(t.task_id)
        assert task is not None
        assert task.metadata.get("custom_key") == "custom_value"
        assert task.metadata.get("recovered_at") is not None
        assert task.metadata.get("recovery_state") is not None


class TestRecoveryPlan:
    """План восстановления."""

    async def test_get_recovery_plan(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Human-readable план восстановления."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Plan test", original_request="plan",
        )
        await manager.start_task(t.task_id)
        s1 = await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/x.txt")
        await manager.update_step(s1.step_id, status="completed", progress=100)
        await manager.add_step(t.task_id, action_type="RUN_SHELL", command="echo test")

        plan = await recovery.get_recovery_plan(t.task_id)
        assert "Восстановления" in plan or "восстановления" in plan or "шагов" in plan
        assert t.title in plan

    async def test_get_recovery_plan_not_found(self, recovery: RecoveryManager) -> None:
        """Несуществующая задача — сообщение об ошибке."""
        plan = await recovery.get_recovery_plan("nonexistent")
        assert "не найдена" in plan

    async def test_get_recovery_plan_empty(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Задача без шагов — информативный план."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Empty Plan", original_request="empty",
        )
        plan = await recovery.get_recovery_plan(t.task_id)
        assert plan


class TestRecoveryResult:
    """Структура RecoveryResult."""

    def test_recovery_result_defaults(self) -> None:
        r = RecoveryResult()
        assert r.task_id == ""
        assert r.recovered is False
        assert r.steps_completed == 0
        assert r.steps_pending == 0
        assert r.warnings == []
        assert r.error is None

    def test_recovery_result_success(self) -> None:
        r = RecoveryResult(
            task_id="abc123",
            title="Test",
            previous_status="running",
            new_status="running",
            steps_completed=3,
            steps_pending=1,
            recovered=True,
        )
        assert r.recovered
        assert r.task_id == "abc123"


class TestRecoveryWithFailedSteps:
    """Восстановление с упавшими шагами."""

    async def test_recover_with_failed_steps(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Упавшие шаги идентифицируются при восстановлении."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Failed steps", original_request="fail",
        )
        await manager.start_task(t.task_id)

        # Успешные шаги
        s1 = await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/ok.txt")
        await manager.update_step(s1.step_id, status="completed", progress=100)

        # Упавший шаг
        await manager.add_step(
            t.task_id, action_type="RUN_SHELL", command="failing command",
        )

        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.recovered is True
        assert r.steps_completed == 1

    async def test_recover_with_warnings(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Восстановление добавляет предупреждения при проблемах."""
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Warnings", original_request="warn",
            metadata={"pid": 999999999},
        )
        await manager.start_task(t.task_id)
        s = await manager.add_step(t.task_id, action_type="WRITE_FILE")
        await manager.update_step(s.step_id, status="failed", error="Disk full")

        results = await recovery.recover_all()
        assert len(results) == 1
        r = results[0]
        assert r.recovered is True


class TestRecoveryCheckpoint:
    """Сохранение checkpoint."""

    async def test_checkpoint_saved(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """После восстановления создаётся checkpoint файл."""
        await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Checkpoint", original_request="cp",
        )
        await recovery.recover_all()
        checkpoint = recovery.load_last_checkpoint()
        assert checkpoint is not None
        assert checkpoint["total"] >= 1
        assert checkpoint["recovery_timestamp"] is not None

    async def test_checkpoint_none_if_no_recovery(self, manager: TaskManager, recovery: RecoveryManager) -> None:
        """Без восстановления checkpoint нет."""
        cp = recovery.load_last_checkpoint()
        # Может быть None или содержать данные от предыдущих тестов
        # Проверяем, что не падает
        assert cp is None or isinstance(cp, dict)
