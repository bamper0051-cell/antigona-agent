"""Tests for the new event-system foundation (antigona.tasks).

Covers:
  - models: Task, TaskStep, TaskStatus, TaskEvent, serialize/deserialize
  - EventBus: subscribe, publish, auto-sequence, wildcard, persist
  - TaskManager: create, get, update, steps, status transitions, persist
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antigona.tasks import (
    TASK_EVENT_TYPES,
    EventBus,
    Task,
    TaskEvent,
    TaskManager,
    TaskStatus,
    TaskStep,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


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


# ── Model tests ──────────────────────────────────────────────────────────────


class TestTaskStatus:
    def test_values(self) -> None:
        assert TaskStatus.QUEUED.value == "queued"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.DONE.value == "done"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.CANCELLED.value == "cancelled"

    def test_terminal(self) -> None:
        assert TaskStatus.DONE.is_terminal
        assert TaskStatus.FAILED.is_terminal
        assert TaskStatus.CANCELLED.is_terminal
        assert not TaskStatus.RUNNING.is_terminal
        assert not TaskStatus.QUEUED.is_terminal

    def test_active(self) -> None:
        assert TaskStatus.QUEUED.is_active
        assert TaskStatus.RUNNING.is_active
        assert not TaskStatus.PAUSED.is_active
        assert not TaskStatus.DONE.is_active

    def test_waiting(self) -> None:
        assert TaskStatus.WAITING_USER.is_waiting
        assert TaskStatus.PAUSED.is_waiting
        assert not TaskStatus.RUNNING.is_waiting


class TestTaskModel:
    def test_create_with_auto_id(self) -> None:
        t = Task(conversation_id=1, owner_user_id=1, title="Test")
        assert t.task_id
        assert t.status == TaskStatus.QUEUED
        assert t.created_at
        assert t.updated_at

    def test_create_with_custom_id(self) -> None:
        t = Task(task_id="custom-123", conversation_id=1, owner_user_id=1)
        assert t.task_id == "custom-123"

    def test_serialize_deserialize(self) -> None:
        original = Task(
            conversation_id=123,
            owner_user_id=456,
            title="Serialization test",
            original_request="do something",
            status=TaskStatus.RUNNING,
            risk_level="HIGH",
            priority=8,
            progress=50,
        )
        data = original.to_dict()
        restored = Task.from_dict(data)
        assert restored.task_id == original.task_id
        assert restored.title == original.title
        assert restored.status == TaskStatus.RUNNING
        assert restored.risk_level == "HIGH"
        assert restored.priority == 8
        assert restored.progress == 50

    def test_deserialize_string_status(self) -> None:
        """Загрузка из JSON — статус приходит строкой."""
        data = {
            "task_id": "abc",
            "conversation_id": 1,
            "owner_user_id": 1,
            "status": "running",
            "steps": [],
        }
        t = Task.from_dict(data)
        assert t.status == TaskStatus.RUNNING
        assert isinstance(t.status, TaskStatus)

    def test_steps_conversion(self) -> None:
        t = Task(
            conversation_id=1,
            owner_user_id=1,
            steps=[
                {"step_id": "s1", "task_id": "", "action_type": "WRITE_FILE"},
                TaskStep(step_id="s2", task_id="", action_type="RUN_SHELL"),
            ],
        )
        assert len(t.steps) == 2
        assert all(isinstance(s, TaskStep) for s in t.steps)
        assert t.steps[0].action_type == "WRITE_FILE"
        assert t.steps[1].action_type == "RUN_SHELL"


class TestTaskStepModel:
    def test_create(self) -> None:
        s = TaskStep(task_id="t1", action_type="WRITE_FILE", path="/tmp/x.txt")
        assert s.step_id
        assert s.status == "pending"
        assert s.max_retries == 3

    def test_serialize_deserialize(self) -> None:
        s = TaskStep(
            task_id="t1",
            action_type="RUN_SHELL",
            command="echo hi",
            status="completed",
            exit_code=0,
            stdout="hi",
        )
        data = s.to_dict()
        restored = TaskStep.from_dict(data)
        assert restored.step_id == s.step_id
        assert restored.command == "echo hi"
        assert restored.exit_code == 0


class TestTaskEventModel:
    def test_create(self) -> None:
        e = TaskEvent(
            event_type="TASK_CREATED",
            task_id="t1",
            conversation_id=1,
            payload={"key": "val"},
        )
        assert e.event_id
        assert e.timestamp
        assert e.to_dict()["payload"]["key"] == "val"

    def test_known_types(self) -> None:
        assert "TASK_CREATED" in TASK_EVENT_TYPES
        assert "STEP_STARTED" in TASK_EVENT_TYPES
        assert len(TASK_EVENT_TYPES) == 22


# ── EventBus tests ────────────────────────────────────────────────────────────


class TestEventBus:
    async def test_singleton(self, tmp_events: str) -> None:
        EventBus.reset_instance()
        b1 = EventBus.get_instance(persist_path=tmp_events)
        b2 = EventBus.get_instance()
        assert b1 is b2

    async def test_publish_and_receive(self, bus: EventBus) -> None:
        received: list[TaskEvent] = []

        async def handler(e: TaskEvent) -> None:
            received.append(e)

        bus.subscribe("TASK_CREATED", handler)
        event = await bus.publish(
            event_type="TASK_CREATED",
            task_id="t1",
            conversation_id=1,
            payload={"msg": "hello"},
        )
        assert len(received) == 1
        assert received[0].event_id == event.event_id
        assert event.payload["msg"] == "hello"

    async def test_auto_sequence(self, bus: EventBus) -> None:
        e1 = await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        e2 = await bus.publish(event_type="B", task_id="t1", conversation_id=1)
        assert e1.sequence == 1
        assert e2.sequence == 2

    async def test_sequence_per_task(self, bus: EventBus) -> None:
        e1 = await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        e2 = await bus.publish(event_type="B", task_id="t2", conversation_id=2)
        assert e1.sequence == 1
        assert e2.sequence == 1  # separate task → separate counter

    async def test_wildcard_subscriber(self, bus: EventBus) -> None:
        received: list[TaskEvent] = []

        async def wh(e: TaskEvent) -> None:
            received.append(e)

        bus.subscribe("*", wh)
        await bus.publish(event_type="CUSTOM_EVENT", task_id="t1", conversation_id=1)
        assert len(received) == 1

    async def test_unsubscribe(self, bus: EventBus) -> None:
        received: list[TaskEvent] = []

        async def handler(e: TaskEvent) -> None:
            received.append(e)

        bus.subscribe("TASK_CREATED", handler)
        await bus.publish(event_type="TASK_CREATED", task_id="t1", conversation_id=1)
        assert len(received) == 1

        bus.unsubscribe("TASK_CREATED", handler)
        await bus.publish(event_type="TASK_CREATED", task_id="t1", conversation_id=1)
        assert len(received) == 1  # not incremented

    async def test_clear_subscribers(self, bus: EventBus) -> None:
        async def h(e: TaskEvent) -> None:
            pass

        bus.subscribe("A", h)
        bus.subscribe("B", h)
        bus.subscribe("*", h)
        bus.clear_subscribers()
        assert bus.stats["subscribers_count"] == 0

    async def test_get_events(self, bus: EventBus) -> None:
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        await bus.publish(event_type="B", task_id="t1", conversation_id=1)
        events = bus.get_events("t1")
        assert len(events) == 2
        assert events[0]["event_type"] == "A"
        assert events[1]["event_type"] == "B"

    async def test_get_events_filtered(self, bus: EventBus) -> None:
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        await bus.publish(event_type="B", task_id="t1", conversation_id=1)
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        a_events = bus.get_events("t1", event_type="A")
        assert len(a_events) == 2

    async def test_get_last_event(self, bus: EventBus) -> None:
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        await bus.publish(event_type="B", task_id="t1", conversation_id=1)
        last = bus.get_last_event("t1")
        assert last is not None
        assert last["event_type"] == "B"

    async def test_count_events(self, bus: EventBus) -> None:
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        await bus.publish(event_type="B", task_id="t1", conversation_id=1)
        assert bus.count_events("t1") == 2

    async def test_handler_error_does_not_crash_bus(self, bus: EventBus) -> None:
        async def broken(_: TaskEvent) -> None:
            raise RuntimeError("oops")

        bus.subscribe("TASK_CREATED", broken)
        # Should not raise
        await bus.publish(event_type="TASK_CREATED", task_id="t1", conversation_id=1)

    async def test_persist_and_reload(self, tmp_events: str) -> None:
        EventBus.reset_instance()
        b1 = EventBus.get_instance(persist_path=tmp_events)
        await b1.publish(event_type="A", task_id="t1", conversation_id=1)
        await b1.publish(event_type="B", task_id="t1", conversation_id=1)
        await b1.publish(event_type="C", task_id="t2", conversation_id=2)

        # New instance reads same file
        EventBus.reset_instance()
        b2 = EventBus.get_instance(persist_path=tmp_events)
        assert b2.count_events("t1") == 2
        assert b2.count_events("t2") == 1
        assert b2.get_events("t1")[0]["event_type"] == "A"

    async def test_clear_persisted(self, bus: EventBus) -> None:
        await bus.publish(event_type="A", task_id="t1", conversation_id=1)
        bus.clear_persisted()
        assert bus.count_events("t1") == 0
        assert bus.stats["total_events"] == 0


# ── TaskManager tests ─────────────────────────────────────────────────────────


class TestTaskManager:
    async def test_create_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(
            conversation_id=111,
            owner_user_id=222,
            title="My task",
            original_request="do it",
        )
        assert t.task_id
        assert t.status == TaskStatus.QUEUED
        assert t.title == "My task"
        assert t.risk_level == "LOW"
        assert t.priority == 5

    async def test_get_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Get me")
        got = await manager.get_task(t.task_id)
        assert got is not None
        assert got.title == "Get me"

    async def test_get_task_not_found(self, manager: TaskManager) -> None:
        assert await manager.get_task("nonexistent") is None

    async def test_get_active_tasks(self, manager: TaskManager) -> None:
        t1 = await manager.create_task(conversation_id=10, owner_user_id=1, title="Active")
        t2 = await manager.create_task(conversation_id=10, owner_user_id=1, title="Done")
        await manager._complete_task_internal(t2.task_id)
        active = await manager.get_active_tasks(10)
        assert len(active) == 1
        assert active[0].task_id == t1.task_id

    async def test_update_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Update")
        # updated_at has second precision, so a back-to-back update within the
        # same second would otherwise be indistinguishable. Pin it to an old
        # value so the update is guaranteed to advance it.
        old_updated_at = "2000-01-01T00:00:00+00:00"
        t.updated_at = old_updated_at
        updated = await manager.update_task(t.task_id, progress=42, risk_level="HIGH")
        assert updated is not None
        assert updated.progress == 42
        assert updated.risk_level == "HIGH"
        assert updated.updated_at != old_updated_at

    async def test_update_task_not_found(self, manager: TaskManager) -> None:
        assert await manager.update_task("nope") is None

    async def test_status_transitions(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Lifecycle")

        await manager.start_task(t.task_id)
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.RUNNING

        await manager.pause_task(t.task_id)
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.PAUSED

        await manager.resume_task(t.task_id)
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.RUNNING

        await manager._complete_task_internal(t.task_id, result="Success!")
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.DONE
        assert t.progress == 100
        assert t.finished_at is not None

    async def test_fail_and_retry(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Fail retry")

        await manager.fail_task(t.task_id, error="oops")
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.FAILED

        await manager.retry_task(t.task_id)
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.RETRYING

    async def test_cancel_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Cancel")
        await manager.cancel_task(t.task_id)
        t = await manager.get_task(t.task_id)
        assert t is not None and t.status == TaskStatus.CANCELLED

    async def test_add_and_update_step(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Steps")
        s = await manager.add_step(
            t.task_id,
            action_type="WRITE_FILE",
            path="/tmp/x.txt",
            content="hello",
        )
        assert s is not None
        assert s.action_type == "WRITE_FILE"
        assert len(await manager.get_steps(t.task_id)) == 1

        # Update step
        updated = await manager.update_step(s.step_id, status="completed", progress=100)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.progress == 100

    async def test_get_step_by_id(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Get step")
        s = await manager.add_step(t.task_id, action_type="RUN_SHELL", command="echo hi")
        found = await manager.get_step(s.step_id)
        assert found is not None
        assert found.command == "echo hi"
        assert await manager.get_step("no-such-step") is None

    async def test_get_recoverable_tasks(self, manager: TaskManager) -> None:
        await manager.create_task(conversation_id=1, owner_user_id=1, title="Queued")
        t2 = await manager.create_task(conversation_id=1, owner_user_id=1, title="Done")
        await manager._complete_task_internal(t2.task_id)
        rec = await manager.get_recoverable_tasks()
        assert len(rec) == 1
        assert rec[0].status == TaskStatus.QUEUED

    async def test_bind_and_find_by_message(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Bind")
        await manager.bind_status_message(t.task_id, 9000)
        found = await manager.find_task_by_message(1, 9000)
        assert found is not None
        assert found.task_id == t.task_id
        # Wrong message id
        assert await manager.find_task_by_message(1, 9999) is None

    async def test_find_by_request(self, manager: TaskManager) -> None:
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Write script",
            original_request="напиши скрипт для бэкапа",
        )
        matches = await manager.get_active_tasks_by_request("скрипт")
        assert len(matches) >= 1
        assert matches[0].task_id == t.task_id
        assert await manager.get_active_tasks_by_request("") == []

    async def test_steer_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(
            conversation_id=1, owner_user_id=1,
            title="Steer",
            original_request="сделай сайт",
        )
        await manager.steer_task(t.task_id, "сделай другой сайт")
        updated = await manager.get_task(t.task_id)
        assert updated is not None
        assert updated.current_request == "сделай другой сайт"

    async def test_set_plan(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Plan")
        await manager.set_plan(t.task_id, "1. Install\n2. Configure\n3. Test")
        updated = await manager.get_task(t.task_id)
        assert updated is not None
        assert updated.plan == "1. Install\n2. Configure\n3. Test"
        assert updated.status == TaskStatus.PLANNING

    async def test_get_tasks_by_status(self, manager: TaskManager) -> None:
        await manager.create_task(conversation_id=1, owner_user_id=1, title="Q")
        t2 = await manager.create_task(conversation_id=1, owner_user_id=1, title="F")
        await manager.fail_task(t2.task_id)
        failed = await manager.get_tasks_by_status(TaskStatus.FAILED)
        assert len(failed) == 1
        assert failed[0].title == "F"

    async def test_delete_task(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Delete me")
        assert await manager.delete_task(t.task_id) is True
        assert await manager.get_task(t.task_id) is None
        # Double delete
        assert await manager.delete_task(t.task_id) is False

    async def test_get_all_tasks_pagination(self, manager: TaskManager) -> None:
        for i in range(5):
            await manager.create_task(conversation_id=1, owner_user_id=1, title=f"T{i}")
        all_t = await manager.get_all_tasks()
        assert len(all_t) == 5
        paginated = await manager.get_all_tasks(limit=2)
        assert len(paginated) == 2

    async def test_get_stats(self, manager: TaskManager) -> None:
        t1 = await manager.create_task(conversation_id=1, owner_user_id=1, title="S1")
        await manager.create_task(conversation_id=2, owner_user_id=1, title="S2")
        await manager._complete_task_internal(t1.task_id)
        stats = await manager.get_stats()
        assert stats["total_tasks"] == 2
        assert stats["active_tasks"] == 1
        assert stats["conversations_count"] == 2

    async def test_render_progress(self, manager: TaskManager) -> None:
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Render")
        await manager.update_task(t.task_id, progress=50)
        t = await manager.get_task(t.task_id)
        assert t is not None
        bar = manager.render_progress(t)
        assert "▓" in bar
        assert "░" in bar
        short = manager.render_short(t)
        assert "Render" in short

    async def test_persistence_survives_restart(self, manager: TaskManager, tmp_persist: Path, bus: EventBus) -> None:
        # Create tasks in first manager
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Survive")
        await manager.add_step(t.task_id, action_type="WRITE_FILE", path="/tmp/x.txt")
        await manager._complete_task_internal(t.task_id, result="Done!")

        # New manager reads same file
        mgr2 = TaskManager(event_bus=bus, tasks_persist_path=tmp_persist)
        await mgr2.start()
        loaded = await mgr2.get_task(t.task_id)
        assert loaded is not None
        assert loaded.title == "Survive"
        assert loaded.status == TaskStatus.DONE
        assert loaded.result == "Done!"
        steps = await mgr2.get_steps(t.task_id)
        assert len(steps) == 1

    async def test_event_publishing_on_create(self, bus: EventBus, manager: TaskManager) -> None:
        received: list[TaskEvent] = []

        async def handler(e: TaskEvent) -> None:
            received.append(e)

        bus.subscribe("TASK_CREATED", handler)
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Events")
        assert len(received) == 1
        assert received[0].task_id == t.task_id

    async def test_event_publishing_on_complete(self, bus: EventBus, manager: TaskManager) -> None:
        received: list[TaskEvent] = []

        async def handler(e: TaskEvent) -> None:
            received.append((e.event_type, e.payload))

        bus.subscribe("TASK_COMPLETED", handler)
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Done")
        await manager._complete_task_internal(t.task_id, result="OK")
        assert len(received) == 1
        assert received[0][0] == "TASK_COMPLETED"

    async def test_event_publishing_on_step(self, bus: EventBus, manager: TaskManager) -> None:
        received: list[str] = []

        async def handler(e: TaskEvent) -> None:
            received.append(e.event_type)

        bus.subscribe("STEP_COMPLETED", handler)
        t = await manager.create_task(conversation_id=1, owner_user_id=1, title="Step event")
        s = await manager.add_step(t.task_id, action_type="WRITE_FILE")
        await manager.update_step(s.step_id, status="completed")
        assert "STEP_COMPLETED" in received
