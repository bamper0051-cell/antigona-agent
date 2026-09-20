"""Lease-recovery worker.

A crashed worker (``kill -9``) leaves two durable footprints: its ``queue_jobs``
row stuck in ``RUNNING`` with an expired lease, and its ``task_flows`` write lease
still nominally held. This worker sweeps both back to a claimable state so a fresh
worker can resume the flow. Duplicate side effects are prevented downstream by the
durable-operation ledger and artifact recovery in the orchestrator, so requeuing an
in-flight step is safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import QueueJob, StateTransition, TaskFlow, TaskState, utcnow
from .state_machine import OBSERVATION_ENTITY_TYPE, TERMINAL_STATES


@dataclass(frozen=True)
class RecoveryReport:
    """What a single recovery sweep reclaimed."""

    requeued_jobs: int
    released_task_leases: int

    def __bool__(self) -> bool:
        return bool(self.requeued_jobs or self.released_task_leases)


class RecoveryWorker:
    def __init__(self, session: Session, *, actor: str = "recovery") -> None:
        self.session = session
        self.actor = actor

    def recover(self) -> RecoveryReport:
        """Requeue expired-lease jobs and release expired task leases, then commit."""
        now = utcnow()
        requeued = self._requeue_expired_jobs(now)
        released = self._release_expired_task_leases(now)
        self.session.commit()
        return RecoveryReport(requeued_jobs=requeued, released_task_leases=released)

    def _requeue_expired_jobs(self, now: datetime) -> int:
        jobs = (
            self.session.scalars(
                select(QueueJob).where(
                    QueueJob.status == "RUNNING",
                    QueueJob.lease_expires_at.is_not(None),
                    QueueJob.lease_expires_at < now,
                )
            )
            .all()
        )
        for job in jobs:
            self.session.add(
                StateTransition(
                    task_id=job.task_id,
                    entity_id=job.id,
                    entity_type="queue",
                    from_state="RUNNING",
                    to_state="QUEUED",
                    reason=f"lease expired; requeued from {job.lease_owner}",
                    actor=self.actor,
                    correlation_id=job.correlation_id,
                )
            )
            job.status = "QUEUED"
            job.lease_owner = None
            job.lease_expires_at = None
            job.available_at = now
            job.last_error = "recovered: lease expired"
        return len(jobs)

    def _release_expired_task_leases(self, now: datetime) -> int:
        tasks = (
            self.session.scalars(
                select(TaskFlow).where(
                    TaskFlow.lease_expires_at.is_not(None),
                    TaskFlow.lease_expires_at < now,
                )
            )
            .all()
        )
        released = 0
        for task in tasks:
            if TaskState(task.status) in TERMINAL_STATES:
                continue
            self.session.add(
                StateTransition(
                    task_id=task.id,
                    entity_id=task.id,
                    # FP-L02: reclaiming an expired lease is not a state change
                    # (the task keeps the state it was found in), so it is an
                    # OBSERVATION, not a fabricated ``X -> X`` transition.
                    entity_type=OBSERVATION_ENTITY_TYPE,
                    from_state=task.status,
                    to_state=task.status,
                    reason=f"expired lease reclaimed from {task.lease_owner}",
                    actor=self.actor,
                    correlation_id=str(uuid.uuid4()),
                )
            )
            task.lease_owner = None
            task.lease_expires_at = None
            released += 1
        return released
