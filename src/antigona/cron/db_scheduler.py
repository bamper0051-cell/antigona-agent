from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from croniter import croniter  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from antigona.models import CronSchedule, ScheduleEvent, TaskFlow, utcnow
from antigona.queue import DurableQueue
from antigona.repository import CreateTask, TaskRepository
from antigona.task_goal import resolve_free_text_request


class CronScheduleNotFound(Exception):
    pass


class CronScheduler:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.last_tick_error_count: int = 0

    # ── Journal helper ──────────────────────────────────────────────

    def _journal(
        self,
        schedule: CronSchedule,
        event_type: str,
        message: str | None = None,
        correlation_id: str = "",
    ) -> ScheduleEvent:
        event = ScheduleEvent(
            schedule_id=schedule.id,
            event_type=event_type,
            message=message,
            correlation_id=correlation_id or "",
        )
        self.session.add(event)
        return event

    # ── CRUD ────────────────────────────────────────────────────────

    def create_schedule(
        self,
        name: str,
        cron_expression: str,
        owner_id: str,
        goal: str,
        target_path: str = "workspace",
        content: str = "",
        tool_name: str | None = None,
        tool_arguments: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        correlation_id: str = "",
    ) -> CronSchedule:
        """Create a durable cron schedule.

        FP-L05b: when ``tool_name`` is omitted the goal is FREE TEXT and the
        contract (tool, target, command, content) is resolved by the canonical
        goal resolver — the same one ``POST /tasks`` and the planner use. The
        old ``workspace.write_text`` default made a scheduled shell goal
        ("запусти в оболочке ls") execute as a write of its own request text,
        which the verifier then refuses.
        """
        if not croniter.is_valid(cron_expression):
            raise ValueError(f"Invalid cron expression: {cron_expression}")
        if tool_name is None:
            request = resolve_free_text_request(goal)
            tool_name = request.tool_name
            # Only an ACTION plan with its own target moves the schedule's
            # target: an effect-free goal keeps the caller's/default path, so a
            # schedule that names no tool never acquires a surprising target.
            if target_path == "workspace" and not request.answer_only and request.path:
                target_path = request.path
            if not content:
                content = request.content or ""
            if request.command:
                tool_arguments = {
                    **(tool_arguments or {}),
                    "command": list(request.command),
                }
        now = utcnow()
        itr = croniter(cron_expression, now)
        next_dt: datetime = itr.get_next(datetime)
        if next_dt.tzinfo is not None:
            next_dt = next_dt.astimezone(UTC).replace(tzinfo=None)

        sid = schedule_id or str(uuid.uuid4())
        schedule = CronSchedule(
            id=sid,
            name=name,
            cron_expression=cron_expression,
            owner_id=owner_id,
            goal=goal,
            target_path=target_path,
            content=content,
            tool_name=tool_name,
            tool_arguments=tool_arguments or {},
            enabled=True,
            cancelled=False,
            last_run_at=None,
            next_run_at=next_dt,
        )
        self.session.add(schedule)
        self._journal(schedule, "created", f"Schedule '{name}' created", correlation_id)
        self.session.commit()
        return schedule

    def get_schedule(self, schedule_id: str, owner_id: str | None = None) -> CronSchedule:
        sched = self.session.get(CronSchedule, schedule_id)
        if not sched:
            raise CronScheduleNotFound(f"CronSchedule {schedule_id} not found")
        if owner_id is not None and sched.owner_id != owner_id:
            raise CronScheduleNotFound(f"CronSchedule {schedule_id} not found")
        return sched

    def list_schedules(self, owner_id: str | None = None) -> Sequence[CronSchedule]:
        stmt = select(CronSchedule).order_by(CronSchedule.created_at.desc())
        if owner_id is not None:
            stmt = stmt.where(CronSchedule.owner_id == owner_id)
        return self.session.scalars(stmt).all()

    def cancel_schedule(
        self, schedule_id: str, owner_id: str | None = None, correlation_id: str = ""
    ) -> CronSchedule:
        sched = self.get_schedule(schedule_id, owner_id=owner_id)
        sched.cancelled = True
        sched.enabled = False
        self._journal(
            sched,
            "cancelled",
            f"Schedule cancelled by {sched.owner_id}",
            correlation_id,
        )
        self.session.commit()
        return sched

    # ── Tick ────────────────────────────────────────────────────────

    def tick(self, correlation_id: str = "") -> list[TaskFlow]:
        self.last_tick_error_count = 0
        now = utcnow()
        due_schedules = self.session.scalars(
            select(CronSchedule).where(
                CronSchedule.enabled.is_(True),
                CronSchedule.cancelled.is_(False),
                CronSchedule.next_run_at <= now,
            )
        ).all()

        created_tasks: list[TaskFlow] = []
        repo = TaskRepository(self.session)
        queue = DurableQueue(self.session)

        for sched in due_schedules:
            if sched.cancelled or not sched.enabled:
                continue
            # FP-L05d: a schedule stored with the removed write default
            # (``workspace.write_text`` + an empty body) must not be replayed as
            # a write of its own request text. Resolve the goal on tick — same
            # canonical resolver ``create_schedule`` and ``POST /tasks`` use.
            tick_tool = sched.tool_name or ""
            tick_path = sched.target_path
            tick_content = sched.content
            tick_command: tuple[str, ...] = ()
            tick_params: dict[str, Any] = dict(sched.tool_arguments or {})
            if tick_tool == "workspace.write_text" and not (tick_content or "").strip():
                request = resolve_free_text_request(sched.goal)
                tick_tool = request.tool_name
                tick_content = request.content or ""
                tick_command = request.command
                if request.path:
                    tick_path = request.path
                if request.answer_only:
                    tick_params = {**tick_params, "answer_only": True}
            idem_key = f"cron:{sched.id}:{sched.next_run_at.isoformat()}"
            try:
                task, created = repo.create(
                    CreateTask(
                        owner_id=sched.owner_id,
                        goal=sched.goal,
                        path=tick_path,
                        content=tick_content,
                        idempotency_key=idem_key,
                        tool_name=tick_tool,
                        command=tick_command,
                        params=tick_params,
                    )
                )
                if created:
                    queue.enqueue(task, correlation_id=correlation_id)
                    created_tasks.append(task)
                    self._journal(
                        sched,
                        "ticked",
                        f"Created task flow {task.id} via cron tick",
                        correlation_id,
                    )
                else:
                    self._journal(
                        sched,
                        "ticked",
                        f"Idempotent hit for task {task.id} (not re-created)",
                        correlation_id,
                    )

                sched.last_run_at = now
                itr = croniter(sched.cron_expression, now)
                next_dt: datetime = itr.get_next(datetime)
                if next_dt.tzinfo is not None:
                    next_dt = next_dt.astimezone(UTC).replace(tzinfo=None)
                sched.next_run_at = next_dt
            except Exception as exc:
                self.last_tick_error_count += 1
                self._journal(
                    sched,
                    "errored",
                    f"Tick error: {exc}",
                    correlation_id,
                )
                # Continue processing other schedules — one error does not block the batch.

        self.session.commit()
        return created_tasks

    # ── Jobs / Events ───────────────────────────────────────────────

    def get_jobs(
        self, schedule_id: str, owner_id: str | None = None
    ) -> list[TaskFlow]:
        """Return TaskFlow instances created by this schedule, via idempotency-key prefix."""
        sched = self.get_schedule(schedule_id, owner_id=owner_id)
        prefix = f"cron:{sched.id}:"
        flows = self.session.scalars(
            select(TaskFlow).where(
                TaskFlow.owner_id == sched.owner_id,
                TaskFlow.idempotency_key.startswith(prefix),
            ).order_by(TaskFlow.created_at.desc())
        ).all()
        return list(flows)

    def get_events(
        self, schedule_id: str, owner_id: str | None = None, limit: int = 50
    ) -> Sequence[ScheduleEvent]:
        sched = self.get_schedule(schedule_id, owner_id=owner_id)
        return self.session.scalars(
            select(ScheduleEvent)
            .where(ScheduleEvent.schedule_id == sched.id)
            .order_by(ScheduleEvent.created_at.desc())
            .limit(limit)
        ).all()
