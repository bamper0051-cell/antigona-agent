"""Durable Execution Kernel (M1) — durable TaskStore + RunStore.

Runtime truth = durable storage. Task and Run are separate entities; a task
never has two active Runs; one atomic lease per task; stale workers are fenced
(they cannot write a terminal state after losing the lease).

Atomicity guarantees live at the STORAGE layer (conditional UPDATE + rowcount,
single SQLite statement, no Python lock), so they hold across processes.

Contract summary (see ARCHITECTURE.md):
* A task has at most one active Run.
* After lease expiry the run is reclaimed (LOST); the previous worker's
  terminal write is fenced (rowcount 0).
* Guarantee is at-least-once; idempotency (where a key is supplied) collapses
  duplicate submissions.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    KernelDependency,
    KernelRun,
    KernelTask,
    KernelTransition,
)
from .state import (
    KernelStateError,
    RunState,
    TaskState,
    require_run_transition,
    require_task_transition,
)

logger = logging.getLogger(__name__)


class KernelNotFoundError(Exception):
    pass


class KernelClaimError(Exception):
    """Claim failed: task already leased / not claimable (not an error case to retry)."""


class KernelFenceError(Exception):
    """Stale worker lost the lease and cannot write a terminal state."""


def _now() -> datetime:
    return datetime.now(UTC)


def _rowcount(result: Any) -> int:
    """Rowcount of a ``session.execute(update/delete)`` CursorResult.

    Avoids the stub typing gap (``Result`` has no ``rowcount`` attribute on
    the mypy surface even though ``CursorResult`` exposes it at runtime).
    """
    return int(getattr(result, "rowcount", 0) or 0)


class KernelStore:
    """Durable, concurrency-safe store for Tasks and Runs.

    Every public operation opens its OWN fresh session from ``session_factory``
    so cross-connection atomicity is exercised exactly as in production (each
    worker/process holds its own DB connection).
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._factory = session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session: Session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── History ────────────────────────────────────────────────────────────

    def _record(
        self,
        session: Session,
        *,
        task_id: str,
        entity_type: str,
        entity_id: str,
        to_state: str,
        from_state: str | None,
        reason: str,
        actor: str,
        run_id: str | None = None,
    ) -> None:
        session.add(
            KernelTransition(
                task_id=task_id,
                run_id=run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                actor=actor,
            )
        )

    # ── Task creation (idempotent) ─────────────────────────────────────────

    def create_task(
        self,
        *,
        owner_id: str,
        kind: str = "generic",
        payload: Mapping[str, Any] | None = None,
        max_attempts: int = 3,
        retryable: bool = True,
        retry_delay_seconds: int = 0,
        idempotency_key: str = "",
        dependencies: Sequence[str] | None = None,
        actor: str = "submit",
    ) -> KernelTask:
        """Create a Task (idempotent on owner+key). Returns the task.

        With a non-empty ``idempotency_key`` a repeat submission returns the
        existing task instead of creating a duplicate — the primitive that
        prevents duplicated side effects (E2E covered by the test suite).
        """
        with self._session() as session:
            if idempotency_key:
                existing = (
                    session.execute(
                        select(KernelTask).where(
                            KernelTask.owner_id == owner_id,
                            KernelTask.idempotency_key == idempotency_key,
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    return existing

            deps = list(dependencies or [])
            task = KernelTask(
                owner_id=owner_id,
                kind=kind,
                payload=dict(payload or {}),
                max_attempts=max(int(max_attempts), 1),
                retryable=bool(retryable),
                retry_delay_seconds=int(retry_delay_seconds),
                idempotency_key=idempotency_key,
            )
            session.add(task)
            try:
                session.flush()
            except IntegrityError:
                # Concurrent duplicate submission with the same
                # (owner_id, idempotency_key) hit the partial unique index.
                # Return the already-inserted task instead of raising.
                session.rollback()
                dup = (
                    session.execute(
                        select(KernelTask).where(
                            KernelTask.owner_id == owner_id,
                            KernelTask.idempotency_key == idempotency_key,
                        )
                    )
                    .scalars()
                    .first()
                )
                if dup is not None:
                    return dup
                raise

            for pid in deps:
                exists = session.execute(
                    select(KernelTask.id).where(KernelTask.id == pid)
                ).first()
                if exists is None:
                    raise KernelNotFoundError(f"parent task not found: {pid}")
                session.add(
                    KernelDependency(child_id=task.id, parent_id=pid)
                )

            unmet = self._unmet_parents(session, task.id)
            initial = TaskState.BLOCKED if unmet else TaskState.READY
            task.status = initial.value
            self._record(
                session,
                task_id=task.id,
                entity_type="task",
                entity_id=task.id,
                from_state=TaskState.PENDING.value,
                to_state=initial.value,
                reason=(
                    "awaiting dependencies" if unmet else "ready"
                ),
                actor=actor,
            )
            session.flush()
            return task

    def _unmet_parents(self, session: Session, task_id: str) -> list[str]:
        rows = session.execute(
            select(KernelDependency.parent_id).where(
                KernelDependency.child_id == task_id
            )
        ).scalars().all()
        unmet = []
        for pid in rows:
            parent = session.get(KernelTask, pid)
            if parent is None or parent.status != TaskState.SUCCEEDED.value:
                unmet.append(pid)
        return unmet

    # ── Atomic claim (one active lease per task) ───────────────────────────

    def claim_task(
        self,
        task_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> KernelTask | None:
        """Atomically claim a READY task. Exactly one of N concurrent callers
        wins (conditional UPDATE + rowcount at the storage layer). Returns the
        claimed task or None when another worker already claimed it.

        A task that has cancellation requested is never claimed.
        """
        ts = now or _now()
        expiry = ts + timedelta(seconds=lease_seconds)
        with self._session() as session:
            res = session.execute(
                update(KernelTask)
                .where(
                    KernelTask.id == task_id,
                    KernelTask.status == TaskState.READY.value,
                    KernelTask.cancel_requested.is_(False),
                    (
                        KernelTask.lease_expires_at.is_(None)
                        | (KernelTask.lease_expires_at < ts)
                    ),
                )
                .values(
                    status=TaskState.RUNNING.value,
                    lease_owner=worker_id,
                    lease_expires_at=expiry,
                    heartbeat_at=ts,
                )
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                return None
            self._record(
                session,
                task_id=task_id,
                entity_type="task",
                entity_id=task_id,
                from_state=TaskState.READY.value,
                to_state=TaskState.RUNNING.value,
                reason=f"claimed by {worker_id}",
                actor=worker_id,
            )
            return session.get(KernelTask, task_id)

    def renew_lease(
        self,
        task_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        """Extend the lease + heartbeat. Fenced: only the current holder can
        renew; after a reclaim the old worker's renew is a no-op."""
        ts = now or _now()
        expiry = ts + timedelta(seconds=lease_seconds)
        with self._session() as session:
            res = session.execute(
                update(KernelTask)
                .where(
                    KernelTask.id == task_id,
                    KernelTask.lease_owner == worker_id,
                    KernelTask.status == TaskState.RUNNING.value,
                )
                .values(lease_expires_at=expiry, heartbeat_at=ts)
                .execution_options(synchronize_session=False)
            )
            return _rowcount(res) == 1

    def renew_run_lease(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        """Extend a RUNNING run's lease + heartbeat while it executes.

        Fenced on lease_owner + RUNNING so a live worker is never reclaimed
        mid-flight (prevents concurrent re-execution). Returns False if the
        run was reclaimed/finished while the worker was away."""
        ts = now or _now()
        expiry = ts + timedelta(seconds=lease_seconds)
        with self._session() as session:
            res = session.execute(
                update(KernelRun)
                .where(
                    KernelRun.id == run_id,
                    KernelRun.lease_owner == worker_id,
                    KernelRun.status == RunState.RUNNING.value,
                )
                .values(lease_expires_at=expiry, heartbeat_at=ts)
                .execution_options(synchronize_session=False)
            )
            return _rowcount(res) == 1

    # ── Run lifecycle ──────────────────────────────────────────────────────

    def create_run(
        self,
        task_id: str,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> KernelRun:
        """Create the next Run for a task. Only the task-lease holder may do
        this (fenced on lease_owner). run_number is max+1; the unique
        (task_id, run_number) constraint prevents duplicate active Runs."""
        ts = now or _now()
        with self._session() as session:
            task = session.get(KernelTask, task_id)
            if task is None:
                raise KernelNotFoundError(f"task not found: {task_id}")
            if task.lease_owner != worker_id or task.status != TaskState.RUNNING.value:
                raise KernelFenceError(
                    f"worker {worker_id} does not hold the task lease; cannot create run"
                )
            last = session.execute(
                select(func.coalesce(func.max(KernelRun.run_number), 0)).where(
                    KernelRun.task_id == task_id
                )
            ).scalar_one()
            run_number = int(last) + 1
            attempt = run_number  # 1-based attempt == run number
            run = KernelRun(
                task_id=task_id,
                run_number=run_number,
                attempt=attempt,
                status=RunState.RUNNING.value,
                lease_owner=worker_id,
                lease_expires_at=ts + timedelta(seconds=lease_seconds),
                heartbeat_at=ts,
                started_at=ts,
            )
            session.add(run)
            session.flush()
            task.attempt_count = max(task.attempt_count, attempt)
            self._record(
                session,
                task_id=task_id,
                entity_type="run",
                entity_id=run.id,
                from_state=RunState.READY.value,
                to_state=RunState.RUNNING.value,
                reason=f"run #{run_number} by {worker_id}",
                actor=worker_id,
                run_id=run.id,
            )
            return run

    def finalize_run(
        self,
        run_id: str,
        worker_id: str,
        to_state: RunState,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        reason: str = "",
        now: datetime | None = None,
    ) -> KernelRun:
        """Fenced terminal write for a Run. A stale worker (lost the lease via
        reclaim) matches 0 rows => KernelFenceError, so it can never report
        SUCCEEDED after losing ownership. Returns the finalised Run."""
        require_run_transition(RunState.RUNNING, to_state)
        ts = now or _now()
        with self._session() as session:
            res = session.execute(
                update(KernelRun)
                .where(
                    KernelRun.id == run_id,
                    KernelRun.lease_owner == worker_id,
                    KernelRun.status == RunState.RUNNING.value,
                )
                .values(
                    status=to_state.value,
                    finished_at=ts,
                    result=dict(result or {}),
                    error=dict(error or {}),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                raise KernelFenceError(
                    f"worker {worker_id} does not hold run {run_id} (fenced)"
                )
            run = session.get(KernelRun, run_id)
            if run is None:  # pragma: no cover - invariant: rowcount==1 => row exists
                raise KernelNotFoundError(f"run vanished: {run_id}")
            self._record(
                session,
                task_id=run.task_id,
                entity_type="run",
                entity_id=run_id,
                from_state=RunState.RUNNING.value,
                to_state=to_state.value,
                reason=reason,
                actor=worker_id,
                run_id=run_id,
            )
            return run

    # ── Task terminal update (after a run finishes) ────────────────────────

    def _release_task_lease(self, session: Session, task_id: str) -> None:
        session.execute(
            update(KernelTask)
            .where(KernelTask.id == task_id)
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )

    def _advance_task_in_session(
        self, session: Session, task_id: str, run: KernelRun, worker_id: str
    ) -> KernelTask:
        """Advance a Task after a Run reaches a terminal state (same session).

        * run SUCCEEDED -> task SUCCEEDED (release lease), children re-evaluated.
        * run FAILED -> CANCELLED if cancel requested, else bounded retry or FAILED.
        * run CANCELLED -> task CANCELLED (never retried).
        * run LOST -> handled by reconciliation (retry or FAILED).
        """
        task = session.get(KernelTask, task_id)
        if task is None:
            raise KernelNotFoundError(f"task not found: {task_id}")
        cur = TaskState(task.status)

        if run.status == RunState.SUCCEEDED.value:
            require_task_transition(cur, TaskState.SUCCEEDED)
            task.status = TaskState.SUCCEEDED.value
            task.result = dict(run.result or {})
            self._record(
                session, task_id=task_id, entity_type="task", entity_id=task_id,
                from_state=cur.value, to_state=TaskState.SUCCEEDED.value,
                reason="run succeeded", actor=worker_id,
            )
        elif run.status == RunState.CANCELLED.value:
            require_task_transition(cur, TaskState.CANCELLED)
            task.status = TaskState.CANCELLED.value
            self._record(
                session, task_id=task_id, entity_type="task", entity_id=task_id,
                from_state=cur.value, to_state=TaskState.CANCELLED.value,
                reason="run cancelled", actor=worker_id,
            )
        elif run.status == RunState.FAILED.value:
            task.attempt_count = max(task.attempt_count, run.attempt)
            err = dict(run.error or {})
            retryable = bool(err.get("retryable", True))
            if task.cancel_requested:
                require_task_transition(cur, TaskState.CANCELLED)
                task.status = TaskState.CANCELLED.value
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=cur.value, to_state=TaskState.CANCELLED.value,
                    reason="cancelled during run", actor=worker_id,
                )
            elif (
                task.retryable
                and retryable
                and task.attempt_count < task.max_attempts
            ):
                require_task_transition(cur, TaskState.READY)
                task.status = TaskState.READY.value
                task.error = err
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=cur.value, to_state=TaskState.READY.value,
                    reason=f"retry scheduled (attempt {task.attempt_count}/{task.max_attempts})",
                    actor=worker_id,
                )
            else:
                require_task_transition(cur, TaskState.FAILED)
                task.status = TaskState.FAILED.value
                task.error = err
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=cur.value, to_state=TaskState.FAILED.value,
                    reason="exhausted retries or non-retryable", actor=worker_id,
                )
        elif run.status == RunState.LOST.value:
            if task.cancel_requested:
                require_task_transition(cur, TaskState.CANCELLED)
                task.status = TaskState.CANCELLED.value
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=cur.value, to_state=TaskState.CANCELLED.value,
                    reason="cancelled; run lost", actor=worker_id,
                )
            # else: leave RUNNING; phase-2 reconciliation decides retry/FAILED
        else:
            raise KernelStateError(f"unexpected run terminal: {run.status}")

        self._release_task_lease(session, task_id)
        session.flush()
        if task.status in (
            TaskState.SUCCEEDED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        ):
            self._reevaluate_children(session, task_id)
        return task

    def complete_task_after_run(
        self,
        task_id: str,
        run: KernelRun,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> KernelTask:
        """Advance the Task after a Run reaches a terminal state."""
        del now  # kept for API symmetry; advancement is pure within a session
        with self._session() as session:
            return self._advance_task_in_session(session, task_id, run, worker_id)

    def finalize_and_complete(
        self,
        task_id: str,
        run_id: str,
        worker_id: str,
        to_state: RunState,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        reason: str = "",
        now: datetime | None = None,
    ) -> KernelRun:
        """Fenced finalize + task advancement in ONE transaction.

        Eliminates the crash window between ``finalize_run`` and
        ``complete_task_after_run`` (review case B): either both land or
        neither does, so a task can never be left RUNNING with a terminal run.
        """
        require_run_transition(RunState.RUNNING, to_state)
        ts = now or _now()
        with self._session() as session:
            res = session.execute(
                update(KernelRun)
                .where(
                    KernelRun.id == run_id,
                    KernelRun.lease_owner == worker_id,
                    KernelRun.status == RunState.RUNNING.value,
                )
                .values(
                    status=to_state.value,
                    finished_at=ts,
                    result=dict(result or {}),
                    error=dict(error or {}),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                raise KernelFenceError(
                    f"worker {worker_id} does not hold run {run_id} (fenced)"
                )
            run = session.get(KernelRun, run_id)
            if run is None:  # pragma: no cover - invariant
                raise KernelNotFoundError(f"run vanished: {run_id}")
            self._record(
                session, task_id=task_id, entity_type="run", entity_id=run_id,
                from_state=RunState.RUNNING.value, to_state=to_state.value,
                reason=reason, actor=worker_id, run_id=run_id,
            )
            self._advance_task_in_session(session, task_id, run, worker_id)
            return run

    def _reevaluate_children(
        self, session: Session, task_id: str, _seen: set[str] | None = None
    ) -> None:
        """When a task reaches SUCCEEDED/FAILED/CANCELLED, update its dependents.

        * all parents SUCCEEDED -> child READY.
        * any parent FAILED/CANCELLED (terminal, non-succeeded) -> child is
          not runnable; mark it CANCELLED (it can never satisfy its deps).

        Recurses into a child that changed state, so a cancelled/failed
        ancestor cascades through the whole graph (A→B→C: C is unblocked too),
        never leaving grandchildren BLOCKED forever. ``_seen`` guards against
        dependency cycles.
        """
        if _seen is None:
            _seen = set()
        if task_id in _seen:
            return
        _seen.add(task_id)
        children = (
            session.execute(
                select(KernelDependency.child_id).where(
                    KernelDependency.parent_id == task_id
                )
            )
            .scalars()
            .all()
        )
        cascade: list[str] = []
        for cid in children:
            child = session.get(KernelTask, cid)
            if child is None or child.status not in (
                TaskState.BLOCKED.value,
                TaskState.PENDING.value,
            ):
                continue
            if self._unmet_parents(session, cid):
                # The child still has an unmet dependency. If ANY parent is
                # terminally failed/cancelled, the child can never satisfy its
                # deps -> cancel it now (never leaves it BLOCKED forever).
                parent_states = self._parent_states(session, cid)
                if parent_states and any(
                    s in (TaskState.FAILED.value, TaskState.CANCELLED.value)
                    for s in parent_states
                ):
                    from_state = child.status
                    child.status = TaskState.CANCELLED.value
                    self._record(
                        session, task_id=cid, entity_type="task", entity_id=cid,
                        from_state=from_state, to_state=TaskState.CANCELLED.value,
                        reason="a dependency failed; child cannot run",
                        actor="kernel",
                    )
                    cascade.append(cid)
                continue
            # All parents succeeded -> ready.
            require_task_transition(TaskState(child.status), TaskState.READY)
            child.status = TaskState.READY.value
            self._record(
                session, task_id=cid, entity_type="task", entity_id=cid,
                from_state=TaskState.BLOCKED.value, to_state=TaskState.READY.value,
                reason="dependencies satisfied", actor="kernel",
            )
            cascade.append(cid)
        # Propagate to the next level of the graph (grandchildren, etc.).
        for cid in cascade:
            self._reevaluate_children(session, cid, _seen)

    def _parent_states(self, session: Session, task_id: str) -> list[str]:
        parents = (
            session.execute(
                select(KernelDependency.parent_id).where(
                    KernelDependency.child_id == task_id
                )
            )
            .scalars()
            .all()
        )
        states = []
        for pid in parents:
            p = session.get(KernelTask, pid)
            if p is not None:
                states.append(p.status)
        return states

    # ── Reconciliation / crash recovery ────────────────────────────────────

    def find_expired_runs(
        self, now: datetime | None = None
    ) -> list[tuple[str, str, int]]:
        """All RUNNING runs whose lease expired: ``(run_id, task_id, attempt)``.

        Returned as plain keys (not ORM objects) so callers never touch a
        detached instance after its session closes.
        """
        ts = now or _now()
        with self._session() as session:
            rows = session.execute(
                select(KernelRun.id, KernelRun.task_id, KernelRun.attempt).where(
                    KernelRun.status == RunState.RUNNING.value,
                    (
                        KernelRun.lease_expires_at.is_(None)
                        | (KernelRun.lease_expires_at < ts)
                    ),
                )
            ).all()
            return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    def reconcile(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> int:
        """Crash recovery: reclaim every expired RUNNING run as LOST and
        advance its task (new run or FAILED per retry policy).

        Runs while the old worker may still believe it owns the run; the old
        worker's terminal write is fenced because this sets lease_owner to the
        reconciler (the old token no longer matches). Returns # runs reclaimed.
        """
        ts = now or _now()
        reclaimed = 0
        for run_id, task_id, attempt in self.find_expired_runs(ts):
            with self._session() as session:
                # Reclaim (fenced): only transitions RUNNING with expired lease.
                res = session.execute(
                    update(KernelRun)
                    .where(
                        KernelRun.id == run_id,
                        KernelRun.status == RunState.RUNNING.value,
                        (
                            KernelRun.lease_expires_at.is_(None)
                            | (KernelRun.lease_expires_at < ts)
                        ),
                    )
                    .values(
                        status=RunState.LOST.value,
                        finished_at=ts,
                        lease_owner=worker_id,
                        error={"code": "LEASE_EXPIRED", "message": "reclaimed by reconciliation"},
                    )
                    .execution_options(synchronize_session=False)
                )
                if _rowcount(res) != 1:
                    continue
                self._record(
                    session, task_id=task_id, entity_type="run", entity_id=run_id,
                    from_state=RunState.RUNNING.value, to_state=RunState.LOST.value,
                    reason="lease expired; reclaimed", actor=worker_id, run_id=run_id,
                )
                task = session.get(KernelTask, task_id)
                if task is None:
                    continue
                self._release_task_lease(session, task.id)
                # Advance task: CANCELLED if requested, else bounded retry/fail.
                task.attempt_count = max(task.attempt_count, attempt)
                if task.cancel_requested:
                    require_task_transition(TaskState(task.status), TaskState.CANCELLED)
                    task.status = TaskState.CANCELLED.value
                    self._record(
                        session, task_id=task.id, entity_type="task", entity_id=task.id,
                        from_state=TaskState.RUNNING.value, to_state=TaskState.CANCELLED.value,
                        reason="cancelled; run lost", actor=worker_id,
                    )
                    session.flush()
                    self._reevaluate_children(session, task.id)
                elif (
                    task.retryable
                    and task.attempt_count < task.max_attempts
                ):
                    require_task_transition(TaskState(task.status), TaskState.READY)
                    task.status = TaskState.READY.value
                    task.error = {"code": "LEASE_EXPIRED", "message": "run lost; retrying"}
                    self._record(
                        session, task_id=task.id, entity_type="task", entity_id=task.id,
                        from_state=TaskState.RUNNING.value, to_state=TaskState.READY.value,
                        reason=f"recovered; retry {task.attempt_count}/{task.max_attempts}",
                        actor=worker_id,
                    )
                else:
                    require_task_transition(TaskState(task.status), TaskState.FAILED)
                    task.status = TaskState.FAILED.value
                    task.error = {"code": "LEASE_EXPIRED", "message": "exhausted retries"}
                    self._record(
                        session, task_id=task.id, entity_type="task", entity_id=task.id,
                        from_state=TaskState.RUNNING.value, to_state=TaskState.FAILED.value,
                        reason="run lost; no retries left", actor=worker_id,
                    )
                    session.flush()
                    self._reevaluate_children(session, task.id)
                reclaimed += 1

        # Phase 2: orphaned TASK leases (crashes between claim_task and
        # create_run, or between finalize_run and complete_task_after_run).
        reclaimed += self._reconcile_orphaned_task_leases(worker_id, ts)
        return reclaimed

    def _reconcile_orphaned_task_leases(
        self, worker_id: str, now: datetime
    ) -> int:
        """Recover RUNNING tasks that have no active RUNNING run.

        Case A: worker claimed the task (RUNNING) then crashed before
          create_run -> no run rows at all.
        Case B: worker finalised a run to a terminal state then crashed
          before complete_task_after_run -> task stuck RUNNING with a
          terminal run.

        Advance the task from its durable run state: SUCCEEDED if any run
        SUCCEEDED; CANCELLED if cancelled; else bounded retry or FAILED.
        Releases the task lease so it can never park RUNNING forever.
        """
        recovered = 0
        with self._session() as session:
            running_ids = (
                session.execute(
                    select(KernelTask.id).where(
                        KernelTask.status == TaskState.RUNNING.value,
                        # Only reclaim tasks whose OWN lease has expired — a
                        # live worker mid-claim (between claim_task and
                        # create_run) still holds a valid lease and must not be
                        # stolen by a concurrent reconciler.
                        or_(
                            KernelTask.lease_expires_at.is_(None),
                            KernelTask.lease_expires_at < now,
                        ),
                    )
                )
                .scalars()
                .all()
            )
        for task_id in running_ids:
            with self._session() as session:
                task = session.get(KernelTask, task_id)
                if task is None or task.status != TaskState.RUNNING.value:
                    continue
                runs = list(
                    session.execute(
                        select(KernelRun)
                        .where(KernelRun.task_id == task_id)
                        .order_by(KernelRun.run_number)
                    )
                    .scalars()
                    .all()
                )
                # A live RUNNING run exists -> the run-reclaim phase owns it.
                if any(r.status == RunState.RUNNING.value for r in runs):
                    continue
                if task.cancel_requested:
                    require_task_transition(TaskState.RUNNING, TaskState.CANCELLED)
                    task.status = TaskState.CANCELLED.value
                    reason = "recovered: cancelled"
                    to_state = TaskState.CANCELLED
                elif any(r.status == RunState.SUCCEEDED.value for r in runs):
                    require_task_transition(TaskState.RUNNING, TaskState.SUCCEEDED)
                    task.status = TaskState.SUCCEEDED.value
                    task.result = dict(runs[-1].result or {}) if runs else {}
                    reason = "recovered: run already succeeded"
                    to_state = TaskState.SUCCEEDED
                elif any(r.status == RunState.CANCELLED.value for r in runs):
                    require_task_transition(TaskState.RUNNING, TaskState.CANCELLED)
                    task.status = TaskState.CANCELLED.value
                    reason = "recovered: run cancelled"
                    to_state = TaskState.CANCELLED
                else:
                    # retryable fail / crash before create_run -> retry or FAIL
                    last_attempt = max((r.attempt for r in runs), default=0)
                    if last_attempt:
                        task.attempt_count = max(task.attempt_count, last_attempt)
                    else:
                        # No run was ever created (crash after claim, or
                        # create_run persistently failing): count this orphaned
                        # claim as one attempt so a task cannot spin
                        # claim->recover->claim forever.
                        task.attempt_count += 1
                    if (
                        task.retryable
                        and task.attempt_count < task.max_attempts
                    ):
                        require_task_transition(TaskState.RUNNING, TaskState.READY)
                        task.status = TaskState.READY.value
                        task.error = {"code": "RECOVERED", "message": "lease reclaimed; retrying"}
                        reason = f"recovered; retry {task.attempt_count}/{task.max_attempts}"
                        to_state = TaskState.READY
                    else:
                        require_task_transition(TaskState.RUNNING, TaskState.FAILED)
                        task.status = TaskState.FAILED.value
                        task.error = {"code": "RECOVERED", "message": "exhausted retries"}
                        reason = "recovered; no retries left"
                        to_state = TaskState.FAILED
                self._release_task_lease(session, task_id)
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=TaskState.RUNNING.value, to_state=to_state.value,
                    reason=reason, actor=worker_id,
                )
                if to_state in (
                    TaskState.SUCCEEDED,
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                ):
                    session.flush()
                    self._reevaluate_children(session, task_id)
                recovered += 1
        return recovered

    # ── Query helpers ───────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> KernelTask | None:
        with self._session() as session:
            return session.get(KernelTask, task_id)

    def get_run(self, run_id: str) -> KernelRun | None:
        with self._session() as session:
            return session.get(KernelRun, run_id)

    def list_runs(self, task_id: str) -> list[KernelRun]:
        with self._session() as session:
            return list(
                session.execute(
                    select(KernelRun)
                    .where(KernelRun.task_id == task_id)
                    .order_by(KernelRun.run_number)
                )
                .scalars()
                .all()
            )

    def list_ready_tasks(
        self, limit: int = 50, now: datetime | None = None
    ) -> list[KernelTask]:
        ts = now or _now()
        with self._session() as session:
            return list(
                session.execute(
                    select(KernelTask)
                    .where(
                        KernelTask.status == TaskState.READY.value,
                        KernelTask.cancel_requested.is_(False),
                        (
                            KernelTask.lease_expires_at.is_(None)
                            | (KernelTask.lease_expires_at < ts)
                        ),
                    )
                    .order_by(KernelTask.created_at)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def history(
        self, task_id: str | None = None, entity_id: str | None = None, limit: int = 100
    ) -> list[KernelTransition]:
        with self._session() as session:
            stmt = select(KernelTransition).order_by(KernelTransition.id)
            if task_id:
                stmt = stmt.where(KernelTransition.task_id == task_id)
            if entity_id:
                stmt = stmt.where(KernelTransition.entity_id == entity_id)
            return list(session.execute(stmt.limit(limit)).scalars().all())

    # ── Cancellation ────────────────────────────────────────────────────────

    def request_cancel(self, task_id: str, actor: str = "owner") -> KernelTask:
        """Set cancel_requested. READY/PENDING/BLOCKED -> CANCELLED directly;
        RUNNING stays RUNNING (cooperative cancel via the executor)."""
        with self._session() as session:
            task = session.get(KernelTask, task_id)
            if task is None:
                raise KernelNotFoundError(f"task not found: {task_id}")
            task.cancel_requested = True
            cur = TaskState(task.status)
            if cur in (TaskState.READY, TaskState.PENDING, TaskState.BLOCKED):
                require_task_transition(cur, TaskState.CANCELLED)
                task.status = TaskState.CANCELLED.value
                self._release_task_lease(session, task_id)
                self._record(
                    session, task_id=task_id, entity_type="task", entity_id=task_id,
                    from_state=cur.value, to_state=TaskState.CANCELLED.value,
                    reason="cancelled before running", actor=actor,
                )
                session.flush()
                # A CANCELLED parent must cancel its dependents (never leave
                # children BLOCKED forever waiting on a cancelled parent).
                self._reevaluate_children(session, task_id)
            return task

    def cancel_claimed_task(self, task_id: str, worker_id: str) -> bool:
        """Fenced-cancel a RUNNING task that was claimed but not yet given a
        run (dispatcher noticed cancel between claim and create_run). Never
        leaves it RUNNING holding a lease; re-evaluates dependents."""
        with self._session() as session:
            task = session.get(KernelTask, task_id)
            if task is None or task.lease_owner != worker_id:
                return False
            if task.status != TaskState.RUNNING.value:
                return False
            require_task_transition(TaskState.RUNNING, TaskState.CANCELLED)
            task.status = TaskState.CANCELLED.value
            self._release_task_lease(session, task_id)
            self._record(
                session, task_id=task_id, entity_type="task", entity_id=task_id,
                from_state=TaskState.RUNNING.value, to_state=TaskState.CANCELLED.value,
                reason="cancelled after claim (no run)", actor=worker_id,
            )
            session.flush()
            self._reevaluate_children(session, task_id)
            return True

    def release_lease(self, task_id: str, worker_id: str) -> bool:
        """Release the task lease early (fenced on lease_owner).

        Used when a run cannot be created so a claimed task is never left
        RUNNING with no run. Returns False if the caller no longer holds the
        lease."""
        with self._session() as session:
            res = session.execute(
                update(KernelTask)
                .where(
                    KernelTask.id == task_id,
                    KernelTask.lease_owner == worker_id,
                )
                .values(lease_owner=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            return _rowcount(res) == 1

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._session() as session:
            t = session.get(KernelTask, task_id)
            return bool(t and t.cancel_requested)

    # ── Cycle detection ─────────────────────────────────────────────────────

    def detect_cycle(self) -> list[list[str]]:
        """Return all dependency cycles (list of task-id chains) or [].

        DFS over kernel_dependencies; any back-edge is a cycle.
        """
        with self._session() as session:
            edges: dict[str, list[str]] = {}
            for parent_id, child_id in session.execute(
                select(KernelDependency.parent_id, KernelDependency.child_id)
            ):
                edges.setdefault(str(parent_id), []).append(str(child_id))
            cycles: list[list[str]] = []
            WHITE, GRAY, BLACK = 0, 1, 2
            color: dict[str, int] = {}

            def dfs(node: str, stack: list[str]) -> None:
                color[node] = GRAY
                stack.append(node)
                for nxt in edges.get(node, []):
                    if color.get(nxt, WHITE) == GRAY:
                        idx = stack.index(nxt)
                        cycle = stack[idx:] + [nxt]
                        if cycle not in cycles:
                            cycles.append(cycle)
                    elif color.get(nxt, WHITE) == WHITE:
                        dfs(nxt, stack)
                stack.pop()
                color[node] = BLACK

            for node in list(edges):
                if color.get(node, WHITE) == WHITE:
                    dfs(node, [])
            return cycles


def claim_many_concurrent(
    store: KernelStore,
    task_id: str,
    workers: Sequence[str],
    lease_seconds: int = 60,
) -> int:
    """Utility for the atomic-claim test: N workers race to claim one task.
    Returns the number of winners (must be exactly 1)."""
    winners = 0
    for wid in workers:
        claimed = store.claim_task(task_id, wid, lease_seconds=lease_seconds)
        if claimed is not None:
            winners += 1
    return winners
