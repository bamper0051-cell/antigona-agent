"""Tests for Cron/Scheduler — планировщик задач."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from antigona.cron.scheduler import (
    Job,
    Scheduler,
    compute_next_run,
    parse_schedule,
)


class TestParseSchedule:
    """Test schedule string parsing."""

    def test_every_n_minutes(self) -> None:
        assert parse_schedule("каждые 30 минут") == "every_30_minutes"
        assert parse_schedule("каждые 5 минут") == "every_5_minutes"
        assert parse_schedule("каждую минуту") == "every_1_minutes"

    def test_every_n_hours(self) -> None:
        assert parse_schedule("каждые 2 часа") == "every_2_hours"
        assert parse_schedule("каждый час") == "every_1_hours"

    def test_cron_expression(self) -> None:
        assert parse_schedule("0 9 * * *") == "cron:0 9 * * *"

    def test_iso_date(self) -> None:
        result = parse_schedule("2026-08-01T09:00")
        assert result.startswith("once_at_")

    def test_invalid_schedule(self) -> None:
        with pytest.raises(ValueError, match="Неизвестный формат"):
            parse_schedule("not-a-schedule")


class TestComputeNextRun:
    """Test next-run computation."""

    def test_every_minute(self) -> None:
        result = compute_next_run("every_30_minutes")
        assert result is not None
        assert result > datetime.now(UTC)

    def test_once_at_future(self) -> None:
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        result = compute_next_run(f"once_at_{future}")
        assert result is not None

    def test_cron(self) -> None:
        result = compute_next_run("cron:0 9 * * *")
        # Should be some time in the future
        assert result is not None


class TestScheduler:
    """Test Scheduler CRUD and lifecycle."""

    def test_create_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("test", "test prompt", "каждые 30 минут")
            assert job.name == "test"
            assert job.prompt == "test prompt"
            assert job.schedule == "every_30_minutes"
            assert job.enabled is True
            assert job.next_run != ""

    def test_create_job_with_cron(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("daily", "daily task", "0 9 * * *")
            assert job.schedule.startswith("cron:")

    def test_list_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            s.create_job("a", "pa", "каждые 30 минут")
            s.create_job("b", "pb", "каждые 1 час")
            jobs = s.list_jobs()
            assert len(jobs) == 2

    def test_list_jobs_only_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            j1 = s.create_job("a", "pa", "каждые 30 минут")
            s.create_job("b", "pb", "каждые 30 минут")
            s.pause_job(j1.id)
            jobs = s.list_jobs(only_enabled=True)
            assert len(jobs) == 1

    def test_pause_resume_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("test", "tp", "каждые 30 минут")
            assert job.enabled is True

            paused = s.pause_job(job.id)
            assert paused is not None
            assert paused.enabled is False

            resumed = s.resume_job(job.id)
            assert resumed is not None
            assert resumed.enabled is True

    def test_pause_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            assert s.pause_job("nonexistent") is None

    def test_remove_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("test", "tp", "каждые 30 минут")
            assert s.remove_job(job.id) is True
            assert s.get_job(job.id) is None

    def test_remove_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            assert s.remove_job("nonexistent") is False

    def test_get_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("test", "tp", "каждые 30 минут")
            fetched = s.get_job(job.id)
            assert fetched is not None
            assert fetched.id == job.id
            assert fetched.name == "test"

    def test_get_job_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            assert s.get_job("nonexistent") is None

    def test_tick_fires_due_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("tick_test", "tp", "каждые 30 минут")
            # Set next_run to the past to force immediate firing
            past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            job.next_run = past

            fired = s.tick()
            assert len(fired) == 1
            assert fired[0].id == job.id
            assert fired[0].run_count == 1
            # Should have rescheduled
            assert fired[0].next_run != ""

    def test_tick_skips_enabled_jobs_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            s.create_job("future", "tp", "каждые 30 минут")
            # next_run is in the future by default
            fired = s.tick()
            assert len(fired) == 0

    def test_tick_skips_paused_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("paused", "tp", "каждые 30 минут")
            s.pause_job(job.id)
            past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            job.next_run = past

            fired = s.tick()
            assert len(fired) == 0

    def test_persistence_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create and save
            s1 = Scheduler(jobs_dir=tmpdir)
            job = s1.create_job("persist", "tp", "каждые 30 минут")

            # Create new instance (simulates restart)
            s2 = Scheduler(jobs_dir=tmpdir)
            jobs = s2.list_jobs()
            assert len(jobs) == 1
            assert jobs[0].id == job.id
            assert jobs[0].name == "persist"

    def test_persistence_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            s.create_job("persist", "tp", "каждые 30 минут")

            # Verify JSON file exists and has correct content
            jobs_file = os.path.join(tmpdir, "jobs.json")
            assert os.path.exists(jobs_file)

            data = json.loads(open(jobs_file).read())
            assert len(data) == 1
            assert data[0]["name"] == "persist"
            assert data[0]["schedule"] == "every_30_minutes"

    def test_callback_on_tick(self) -> None:
        callback_results: list[str] = []

        def callback(job: Job) -> None:
            callback_results.append(job.name)

        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            s.set_callback(callback)

            job = s.create_job("cb_test", "tp", "каждые 30 минут")
            past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            job.next_run = past

            fired = s.tick()
            assert len(fired) == 1
            assert len(callback_results) == 1
            assert callback_results[0] == "cb_test"

    def test_run_job_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Scheduler(jobs_dir=tmpdir)
            job = s.create_job("immediate", "tp", "каждые 30 минут")
            assert job.run_count == 0

            s.run_job(job.id)
            assert job.run_count == 1


class TestJobModel:
    """Test Job dataclass serialization."""

    def test_to_dict(self) -> None:
        job = Job(
            id="test-id",
            name="test",
            prompt="test prompt",
            schedule="every_30_minutes",
            next_run="2026-08-01T09:00:00",
        )
        d = job.to_dict()
        assert d["id"] == "test-id"
        assert d["name"] == "test"
        assert d["prompt"] == "test prompt"
        assert d["schedule"] == "every_30_minutes"
        assert d["next_run"] == "2026-08-01T09:00:00"
        assert d["enabled"] is True
        assert d["run_count"] == 0

    def test_from_dict(self) -> None:
        data = {
            "id": "test-id",
            "name": "test",
            "prompt": "test prompt",
            "schedule": "every_30_minutes",
            "next_run": "2026-08-01T09:00:00",
            "enabled": True,
            "skill": "",
            "created_at": "2026-07-27T12:00:00",
            "last_run": None,
            "run_count": 5,
        }
        job = Job.from_dict(data)
        assert job.id == "test-id"
        assert job.name == "test"
        assert job.run_count == 5
