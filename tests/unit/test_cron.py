from __future__ import annotations

from datetime import timedelta

import pytest

from antigona.cron import CronScheduleNotFound, CronScheduler
from antigona.database import Database
from antigona.models import ScheduleEvent, utcnow
from antigona.queue import DurableQueue


def test_create_schedule_validates_cron() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        with pytest.raises(ValueError, match="Invalid cron expression"):
            scheduler.create_schedule(
                name="bad",
                cron_expression="not-a-cron",
                owner_id="o",
                goal="g",
            )


def test_create_schedule_sets_next_run_at() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="future",
            cron_expression="0 0 * * *",  # midnight daily
            owner_id="o",
            goal="g",
        )
        assert sched.next_run_at > utcnow()


def test_create_schedule_journal() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="journaled",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
            correlation_id="corr-create",
        )
        events = session.query(ScheduleEvent).filter(
            ScheduleEvent.schedule_id == sched.id
        ).all()
        assert len(events) == 1
        assert events[0].event_type == "created"
        assert events[0].correlation_id == "corr-create"


def test_tick_creates_task_flow() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)

        sched = scheduler.create_schedule(
            name="periodic_cleanup",
            cron_expression="* * * * *",
            owner_id="owner-cron",
            goal="Clean temporary files",
        )
        assert sched.name == "periodic_cleanup"
        assert sched.enabled is True
        assert sched.cancelled is False

        # Set next_run_at to past to simulate due job
        sched.next_run_at = utcnow() - timedelta(minutes=5)
        session.commit()

        # Tick scheduler -> puts periodic task into queue_jobs
        tasks = scheduler.tick()
        assert len(tasks) == 1
        created_task = tasks[0]
        assert created_task.goal == "Clean temporary files"

        # Worker claims job from queue_jobs
        queue = DurableQueue(session)
        claimed_job = queue.claim(worker="worker-alpha")

        assert claimed_job is not None
        assert claimed_job.task_id == created_task.id
        assert claimed_job.lease_owner == "worker-alpha"
        assert claimed_job.status == "RUNNING"


def test_tick_idempotency() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="idem",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
        )
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

        # First tick → 1 task
        tasks1 = scheduler.tick()
        assert len(tasks1) == 1

        # Second tick without advancing time → 0 new tasks (idempotency)
        tasks2 = scheduler.tick()
        assert len(tasks2) == 0


def test_tick_skips_cancelled() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="cancel_test",
            cron_expression="* * * * *",
            owner_id="owner-cron",
            goal="Task to cancel",
        )
        scheduler.cancel_schedule(sched.id)
        assert sched.cancelled is True
        assert sched.enabled is False

        sched.next_run_at = utcnow() - timedelta(minutes=5)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 0


def test_tick_skips_disabled() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="disabled",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
        )
        sched.enabled = False
        sched.next_run_at = utcnow() - timedelta(minutes=5)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 0


def test_cancel_is_sticky() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="sticky",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
        )
        scheduler.cancel_schedule(sched.id)
        assert sched.cancelled is True
        assert sched.enabled is False

        # Cannot re-enable via public API
        sched.enabled = True
        session.commit()
        sched.next_run_at = utcnow() - timedelta(minutes=5)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 0  # still cancelled


def test_list_schedules_owner_filter() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        scheduler.create_schedule(name="a", cron_expression="* * * * *", owner_id="o1", goal="g")
        scheduler.create_schedule(name="b", cron_expression="* * * * *", owner_id="o2", goal="g")
        scheduler.create_schedule(name="c", cron_expression="* * * * *", owner_id="o1", goal="g")

        o1_schedules = scheduler.list_schedules(owner_id="o1")
        assert len(o1_schedules) == 2
        o2_schedules = scheduler.list_schedules(owner_id="o2")
        assert len(o2_schedules) == 1


def test_get_jobs_returns_flow_ids() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="jobs_test",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
        )
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 1

        jobs = scheduler.get_jobs(sched.id, owner_id="o")
        assert len(jobs) == 1
        assert jobs[0].id == tasks[0].id


def test_tick_error_does_not_block_others() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)

        # Schedule A — good
        a = scheduler.create_schedule(name="a", cron_expression="* * * * *", owner_id="o", goal="ga")
        a.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

        # Schedule B — good too; both should tick normally with zero errors.
        b = scheduler.create_schedule(name="b", cron_expression="* * * * *", owner_id="o", goal="gb")
        b.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 2  # both ticked successfully
        assert scheduler.last_tick_error_count == 0


def test_tick_malformed_target_path_errors_without_blocking_batch() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)

        good = scheduler.create_schedule(name="good", cron_expression="* * * * *", owner_id="o", goal="ga")
        good.next_run_at = utcnow() - timedelta(minutes=1)

        # Malformed directly in the DB — bypasses schema/ORM defaults and the
        # public API, simulating data that predates the "workspace" default.
        bad = scheduler.create_schedule(name="bad", cron_expression="* * * * *", owner_id="o", goal="gb")
        bad.target_path = "."
        bad.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()

        tasks = scheduler.tick()
        assert len(tasks) == 1
        assert tasks[0].goal == "ga"
        assert scheduler.last_tick_error_count == 1

        bad_events = scheduler.get_events(bad.id, owner_id="o")
        assert any(e.event_type == "errored" for e in bad_events)


def test_schedule_event_journal() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)

        # Create → created event
        sched = scheduler.create_schedule(
            name="journal_me",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
            correlation_id="c1",
        )
        events = session.query(ScheduleEvent).filter(
            ScheduleEvent.schedule_id == sched.id
        ).order_by(ScheduleEvent.created_at).all()
        assert len(events) == 1
        assert events[0].event_type == "created"

        # Tick → ticked event
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()
        scheduler.tick(correlation_id="c2")

        events = session.query(ScheduleEvent).filter(
            ScheduleEvent.schedule_id == sched.id
        ).order_by(ScheduleEvent.created_at).all()
        assert len(events) >= 2
        assert events[-1].event_type in ("ticked",)

        # Cancel → cancelled event
        scheduler.cancel_schedule(sched.id, correlation_id="c3")
        events = session.query(ScheduleEvent).filter(
            ScheduleEvent.schedule_id == sched.id
        ).order_by(ScheduleEvent.created_at).all()
        assert len(events) >= 3
        assert events[-1].event_type == "cancelled"


def test_get_schedule_owner_guard() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="owned",
            cron_expression="* * * * *",
            owner_id="owner-a",
            goal="g",
        )
        # Own owner can see it
        assert scheduler.get_schedule(sched.id, owner_id="owner-a")
        # Different owner gets 404
        with pytest.raises(CronScheduleNotFound):
            scheduler.get_schedule(sched.id, owner_id="owner-b")


def test_cancel_schedule_owner_guard() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="owned",
            cron_expression="* * * * *",
            owner_id="owner-a",
            goal="g",
        )
        # Wrong owner cannot cancel
        with pytest.raises(CronScheduleNotFound):
            scheduler.cancel_schedule(sched.id, owner_id="owner-b")


def test_get_events() -> None:
    db = Database("sqlite:///:memory:")
    db.create_all()
    with db.session_factory() as session:
        scheduler = CronScheduler(session)
        sched = scheduler.create_schedule(
            name="events",
            cron_expression="* * * * *",
            owner_id="o",
            goal="g",
            correlation_id="c1",
        )
        sched.next_run_at = utcnow() - timedelta(minutes=1)
        session.commit()
        scheduler.tick(correlation_id="c2")
        scheduler.cancel_schedule(sched.id, correlation_id="c3")

        events = scheduler.get_events(sched.id, owner_id="o")
        assert len(events) == 3
        types = [e.event_type for e in events]
        assert types == ["cancelled", "ticked", "created"]
