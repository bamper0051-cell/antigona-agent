from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..durable.state_cache import StateCache
from ..models import QueueJob, TaskFlow, TaskState, utcnow
from .redis_broker import (
    DEFAULT_LANE,
    InMemoryBroker,
    NullBroker,
    RedisTaskBroker,
    TaskBroker,
    build_broker,
)

__all__ = [
    "DEFAULT_LANE",
    "DurableQueue",
    "InMemoryBroker",
    "NullBroker",
    "RedisTaskBroker",
    "TaskBroker",
    "build_broker",
]


class DurableQueue:
    """Lease + CAS queue on ``queue_jobs`` — the single source of truth.

    An optional :class:`TaskBroker` is signalled after a successful commit so a
    waiting worker wakes up immediately instead of polling. The signal carries no
    authority: a worker that receives it still has to win ``claim()`` in SQL, and
    a signal that is lost (Redis down, message dropped) only costs latency.
    """

    def __init__(
        self,
        session: Session,
        broker: TaskBroker | None = None,
        state_cache: StateCache | None = None,
    ) -> None:
        self.session = session
        self.broker = broker
        # Passed through to the repository so the RECEIVED -> QUEUED transition
        # publishes to the cache like every other transition does.
        self.state_cache = state_cache

    def _signal(self, task_id: str, lane: str) -> None:
        if self.broker is not None:
            self.broker.signal(task_id, lane=lane)

    def enqueue(self,task:TaskFlow,lane:str="main",correlation_id:str|None=None)->QueueJob:
        existing=self.session.scalar(select(QueueJob).where(QueueJob.task_id==task.id))
        if existing:
            if existing.status in {"DONE","WAITING"} and task.status not in {TaskState.DONE.value,TaskState.CANCELLED.value,TaskState.FAILED.value,TaskState.POLICY_DENIED.value}:
                existing.status="QUEUED"; existing.available_at=utcnow(); self.session.commit()
                self._signal(existing.task_id, existing.lane)
            return existing
        job=QueueJob(task_id=task.id,lane=lane,correlation_id=correlation_id or str(uuid.uuid4()))
        self.session.add(job)
        from ..repository import TaskRepository
        TaskRepository(self.session,self.state_cache).transition(task,TaskState.QUEUED,"durable queue enqueue","gateway",correlation_id=job.correlation_id)
        self.session.commit()
        self._signal(job.task_id, job.lane)
        return job
    def claim(self,worker:str,lease_seconds:int=30)->QueueJob|None:
        now=utcnow()
        candidate=self.session.scalar(select(QueueJob).where(QueueJob.available_at<=now,or_(QueueJob.status=="QUEUED",(QueueJob.status=="RUNNING")&(QueueJob.lease_expires_at<now))).order_by(QueueJob.created_at).limit(1))
        if not candidate: return None
        result=self.session.execute(update(QueueJob).where(QueueJob.id==candidate.id,or_(QueueJob.status=="QUEUED",QueueJob.lease_expires_at<now)).values(status="RUNNING",lease_owner=worker,lease_expires_at=now+timedelta(seconds=lease_seconds),heartbeat_at=now,attempts=QueueJob.attempts+1))
        assert isinstance(result,CursorResult)
        if result.rowcount!=1: self.session.rollback(); return None
        self.session.commit(); return self.session.get(QueueJob,candidate.id)
    def heartbeat(self,job:QueueJob,worker:str,lease_seconds:int=30)->None:
        now=utcnow(); result=self.session.execute(update(QueueJob).where(QueueJob.id==job.id,QueueJob.lease_owner==worker,QueueJob.status=="RUNNING").values(heartbeat_at=now,lease_expires_at=now+timedelta(seconds=lease_seconds)))
        assert isinstance(result,CursorResult)
        if result.rowcount!=1: raise RuntimeError("queue lease lost")
        self.session.commit()
    def finish(self,job:QueueJob)->None: job.status="DONE"; job.lease_owner=None; job.lease_expires_at=None; self.session.commit()
    def retry(self,job:QueueJob,error:str,delay_seconds:int=1)->None:
        job.status="QUEUED"; job.last_error=error; job.available_at=utcnow()+timedelta(seconds=delay_seconds); job.lease_owner=None; job.lease_expires_at=None
        self.session.commit()
        self._signal(job.task_id, job.lane)
