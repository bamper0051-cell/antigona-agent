from __future__ import annotations

import threading

from sqlalchemy.orm import Session, sessionmaker

from .models import QueueJob, TaskFlow
from .queue import DurableQueue
from .repository import LeaseConflict, TaskRepository
from .shell import DockerShellTool


class ExecutionGuard:
    """Renews both leases and turns durable cancellation into container cancellation."""

    def __init__(self, factory: sessionmaker[Session], job_id: str, task_id: str, worker: str,
                 lease_seconds: int, shell: DockerShellTool | None) -> None:
        self.factory=factory; self.job_id=job_id; self.task_id=task_id; self.worker=worker
        self.lease_seconds=lease_seconds; self.shell=shell; self._stop=threading.Event()
        self.thread=threading.Thread(target=self._watch,name=f"lease-{worker}",daemon=True)

    def start(self)->None: self.thread.start()
    def stop(self)->None: self._stop.set(); self.thread.join(timeout=max(2,self.lease_seconds))

    def _watch(self)->None:
        interval=min(1.0,max(0.1,self.lease_seconds/3))
        while not self._stop.wait(interval):
            with self.factory() as session:
                job=session.get(QueueJob,self.job_id); task=session.get(TaskFlow,self.task_id)
                if not job or not task: return
                try:
                    DurableQueue(session).heartbeat(job,self.worker,self.lease_seconds)
                    if task.lease_owner==self.worker: TaskRepository(session).heartbeat(task,self.worker,self.lease_seconds)
                except (RuntimeError,LeaseConflict): return
                if task.cancellation_requested:
                    if self.shell:
                        if task.side_effect_key: self.shell.cancel_execution(task.side_effect_key)
                        else: self.shell.cancel()
                    return