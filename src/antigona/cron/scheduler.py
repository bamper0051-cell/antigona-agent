"""Cron/Scheduler — планировщик задач с JSON-персистентностью.

Предоставляет Job-модель и Scheduler с tick()-циклом.
Состояние сохраняется в JSON-файл и переживает рестарты бота.

Форматы schedule:
  - "каждые N минут" / "каждые N часов"
  - cron-выражение: "0 9 * * *"
  - ISO-дата: "2026-08-01T09:00"
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from antigona.core import paths

logger = logging.getLogger(__name__)


# ─── Schedule parsing utilities ───────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"(?:каждые?|каждую|каждый)\s*(\d+)?\s*(минут[аыеу]?|час[аов]?|секунд[аыу]?|дн[яей]?)",
    re.IGNORECASE,
)

_UNIT_MAP: dict[str, str] = {
    "минут": "minutes",
    "минута": "minutes",
    "минуты": "minutes",
    "минуту": "minutes",
    "час": "hours",
    "часа": "hours",
    "часов": "hours",
    "секунд": "seconds",
    "секунда": "seconds",
    "секунду": "seconds",
    "день": "days",
    "дня": "days",
    "дней": "days",
}


def parse_schedule(schedule_str: str) -> str:
    """Parse a human-readable schedule string into a canonical form.

    Returns an ISO-like description that the Scheduler can interpret.
    Supported inputs:
      - "каждые 30 минут" → timedelta interval
      - "0 9 * * *" → cron expression (passthrough)
      - "2026-08-01T09:00" → ISO date (one-shot)
    """
    s = schedule_str.strip()

    # Try interval pattern
    m = _INTERVAL_RE.match(s)
    if m:
        amount = int(m.group(1)) if m.group(1) else 1
        unit_ru = m.group(2).lower() if m.group(2) else "minutes"
        unit_en = _UNIT_MAP.get(unit_ru, "minutes")
        return f"every_{amount}_{unit_en}"

    # Try ISO date
    if "T" in s:
        try:
            datetime.fromisoformat(s)
            return f"once_at_{s}"
        except ValueError:
            pass

    # Try cron (simple check — 5 space-separated fields)
    parts = s.split()
    if len(parts) == 5 and all(
        (not p) or p.replace("*", "").replace("/", "").replace("-", "").replace(",", "").isdigit()
        or p == "*"
        for p in parts
    ):
        return f"cron:{s}"

    raise ValueError(f"Неизвестный формат расписания: {schedule_str!r}")


def compute_next_run(schedule_key: str) -> datetime | None:
    """Compute the next datetime when a schedule should fire.

    Returns None for one-shot schedules that have fired.
    """
    now = datetime.now(UTC)

    if schedule_key.startswith("every_"):
        parts = schedule_key.split("_")
        if len(parts) >= 3:
            amount = int(parts[1])
            unit = parts[2]
            kwargs = {unit: amount}
            return now + timedelta(**kwargs)
        return now + timedelta(minutes=30)

    if schedule_key.startswith("once_at_"):
        iso_str = schedule_key[8:]
        try:
            return datetime.fromisoformat(iso_str)
        except ValueError:
            return None

    if schedule_key.startswith("cron:"):
        # Simple cron-like: use croniter if available, else approximate
        try:
            from croniter import croniter as _croniter  # type: ignore[import-untyped]

            expr = schedule_key[5:]
            if _croniter.is_valid(expr):
                itr = _croniter(expr, now)
                nxt = itr.get_next(datetime)
                if nxt.tzinfo is not None:
                    nxt = nxt.astimezone(UTC).replace(tzinfo=None)
                return cast(datetime, nxt)
        except ImportError:
            pass
        # Fallback: return 1 hour from now
        return now + timedelta(hours=1)

    return now + timedelta(minutes=30)


# ─── Job model ────────────────────────────────────────────────────────────────


# ── SILENT response marker ───────────────────────────────────────────────────────

SILENT_MARKER = "[SILENT]"


def is_silent_response(text: str) -> bool:
    """Check if a response text contains only the [SILENT] marker."""
    return text.strip() == SILENT_MARKER


# ── Delivery targets ──────────────────────────────────────────────────────────────

VALID_DELIVERY_TARGETS = frozenset({"origin", "telegram", "local"})


# ── Job model ─────────────────────────────────────────────────────────────────────


@dataclass
class Job:
    """A single scheduled job."""

    id: str
    name: str
    prompt: str
    schedule: str  # canonical schedule key
    next_run: str  # ISO format datetime string
    enabled: bool = True
    skill: str = ""
    script_path: str = ""  # optional script to run pre-tick; its stdout becomes context
    delivery_target: str = "origin"  # 'origin', 'telegram', 'local'
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run: str | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "next_run": self.next_run,
            "enabled": self.enabled,
            "skill": self.skill,
            "script_path": self.script_path,
            "delivery_target": self.delivery_target,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "run_count": self.run_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(
            id=data["id"],
            name=data["name"],
            prompt=data["prompt"],
            schedule=data["schedule"],
            next_run=data.get("next_run", ""),
            enabled=data.get("enabled", True),
            skill=data.get("skill", ""),
            script_path=data.get("script_path", ""),
            delivery_target=data.get("delivery_target", "origin"),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            last_run=data.get("last_run"),
            run_count=data.get("run_count", 0),
        )

    def run_script(self) -> str:
        """If script_path is set, run it and return stdout as context.

        Returns empty string on any error or if no script is configured.
        """
        if not self.script_path:
            return ""
        script = Path(self.script_path).expanduser().resolve()
        if not script.is_file():
            logger.warning("Job %s: script not found: %s", self.id, self.script_path)
            return ""
        if not os.access(str(script), os.X_OK):
            logger.warning("Job %s: script not executable: %s", self.id, self.script_path)
            return ""
        try:
            import subprocess as _sp

            result = _sp.run(
                [str(script)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            logger.warning(
                "Job %s: script %s exited %d: %s",
                self.id, self.script_path, result.returncode, result.stderr.strip(),
            )
        except Exception as exc:
            logger.error("Job %s: script error: %s", self.id, exc)
        return ""


# ─── Scheduler ────────────────────────────────────────────────────────────────


class Scheduler:
    """Persistent job scheduler with JSON storage.

    Attributes:
        jobs_dir: Directory for storing the jobs.json file.
        jobs_file: Path to the JSON file with job definitions.
        _jobs: In-memory dict of job_id -> Job.
        _lock: Thread lock for safe concurrent access.
        _callback: Optional callback invoked when a job fires during tick().
    """

    def __init__(self, jobs_dir: str | Path = "") -> None:
        if not jobs_dir:
            jobs_dir = os.environ.get(
                "ANTIGONA_JOBS_DIR",
                str(paths.owner_dir()),
            )
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_file = self.jobs_dir / "jobs.json"
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._callback: Callable[[Job], None] | None = None
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load jobs from JSON file."""
        if self.jobs_file.exists():
            try:
                data = json.loads(self.jobs_file.read_text())
                self._jobs = {}
                for entry in data:
                    job = Job.from_dict(entry)
                    self._jobs[job.id] = job
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load jobs.json: %s", exc)
                self._jobs = {}

    def _save(self) -> None:
        """Save jobs to JSON file."""
        data = [job.to_dict() for job in self._jobs.values()]
        self.jobs_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def set_callback(self, callback: Callable[[Job], None] | None) -> None:
        """Set callback invoked when a job fires during tick()."""
        self._callback = callback

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create_job(
        self,
        name: str,
        prompt: str,
        schedule: str,
        skill: str = "",
        script_path: str = "",
        delivery_target: str = "origin",
    ) -> Job:
        """Create a new scheduled job.

        Args:
            name: Human-readable job name.
            prompt: The prompt/action to execute when the job fires.
            schedule: Schedule string — "каждые 30 минут", "0 9 * * *", "2026-08-01T09:00".
            skill: Optional skill name to associate.
            script_path: Optional path to an executable script run pre-tick.
            delivery_target: Where to deliver results — 'origin', 'telegram', 'local'.

        Returns:
            The created Job instance.
        """
        schedule_key = parse_schedule(schedule)
        next_dt = compute_next_run(schedule_key)

        if delivery_target not in VALID_DELIVERY_TARGETS:
            delivery_target = "origin"

        job = Job(
            id=str(uuid.uuid4()),
            name=name,
            prompt=prompt,
            schedule=schedule_key,
            next_run=next_dt.isoformat() if next_dt else "",
            enabled=True,
            skill=skill,
            script_path=script_path,
            delivery_target=delivery_target,
        )

        with self._lock:
            self._jobs[job.id] = job
            self._save()

        logger.info("Created job %s (%s): %s", job.id, job.name, job.schedule)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, only_enabled: bool = False) -> list[Job]:
        """List all jobs. Optionally filter to enabled only."""
        with self._lock:
            jobs = list(self._jobs.values())
        if only_enabled:
            jobs = [j for j in jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def pause_job(self, job_id: str) -> Job | None:
        """Pause a job (disable it without removing)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.enabled = False
            self._save()
        logger.info("Paused job %s (%s)", job_id, job.name)
        return job

    def resume_job(self, job_id: str) -> Job | None:
        """Resume a paused job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.enabled = True
            # Recompute next run
            next_dt = compute_next_run(job.schedule)
            job.next_run = next_dt.isoformat() if next_dt else ""
            self._save()
        logger.info("Resumed job %s (%s)", job_id, job.name)
        return job

    def run_job(self, job_id: str) -> Job | None:
        """Immediately trigger a job and reschedule it."""
        return self._fire_job(job_id)

    def remove_job(self, job_id: str) -> bool:
        """Remove a job entirely."""
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
            self._save()
        logger.info("Removed job %s", job_id)
        return True

    def edit_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        schedule: str | None = None,
        script_path: str | None = None,
        delivery_target: str | None = None,
    ) -> Job | None:
        """Edit fields of an existing job.

        Args:
            job_id: The job ID to edit.
            name: New name or None to keep.
            prompt: New prompt or None to keep.
            schedule: New schedule string or None to keep.
            script_path: New script path or None to keep.
            delivery_target: New delivery target or None to keep.

        Returns:
            The updated Job, or None if not found.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None

            if name is not None:
                job.name = name
            if prompt is not None:
                job.prompt = prompt
            if script_path is not None:
                job.script_path = script_path
            if delivery_target is not None:
                if delivery_target not in VALID_DELIVERY_TARGETS:
                    delivery_target = "origin"
                job.delivery_target = delivery_target
            if schedule is not None:
                schedule_key = parse_schedule(schedule)
                job.schedule = schedule_key
                next_dt = compute_next_run(schedule_key)
                job.next_run = next_dt.isoformat() if next_dt else ""

            self._save()

        logger.info("Edited job %s (%s)", job_id, job.name)
        return job

    # ── Tick / Fire ───────────────────────────────────────────────────────

    def _fire_job(self, job_id: str) -> Job | None:
        """Fire a single job: invoke callback and reschedule.

        Returns the updated Job or None if not found.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if not job.enabled:
                return job

            # Mark as run
            job.last_run = datetime.now(UTC).isoformat()
            job.run_count += 1

            # Reschedule
            next_dt = compute_next_run(job.schedule)
            job.next_run = next_dt.isoformat() if next_dt else ""

        # Invoke callback outside lock
        if self._callback:
            try:
                self._callback(job)
            except Exception as exc:
                logger.error("Callback error for job %s: %s", job_id, exc)

        self._save()
        return job

    def tick(self) -> list[Job]:
        """Check which jobs are due and fire them.

        Returns a list of jobs that were fired this tick.
        """
        fired: list[Job] = []

        with self._lock:
            job_ids = list(self._jobs.keys())

        for job_id in job_ids:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or not job.enabled:
                    continue
                if not job.next_run:
                    continue
                try:
                    next_dt = datetime.fromisoformat(job.next_run)
                except ValueError:
                    continue
                if next_dt > datetime.now(UTC):
                    continue

            # Fire outside lock
            fired_job = self._fire_job(job_id)
            if fired_job:
                fired.append(fired_job)

        return fired


# ─── Global scheduler instance ────────────────────────────────────────────────

_scheduler: Scheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    """Get or create the global scheduler singleton."""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = Scheduler()
    return _scheduler
