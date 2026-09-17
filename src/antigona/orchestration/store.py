"""Autonomous Goal Orchestration (M2) — durable store.

GoalStore / FlowStore / WakeStore / HealthStore / HandoffStore in one class,
each public operation on a fresh session (atomic across processes).

Guarantees:
* Goal status transitions are CAS (``WHERE status=:from``) — no lost updates.
* Flow updates are **optimistic-revision CAS** (``WHERE revision=:expected``);
  a stale writer (E2E-8) gets :class:`FlowConflictError` and its result is
  rejected instead of overwriting newer truth.
* WAIT is durable: the goal is WAITING, the worker is released, and only a
  persisted WakeEvent (or timer) resumes it — no token-burning polls.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Goal,
    GoalFlow,
    ServiceHandoff,
    ServiceHealthState,
    WakeEvent,
)
from .state import (
    GOAL_TERMINAL,
    FlowState,
    GoalState,
    GoalStateError,
    ServiceState,
    WakeKind,
    WakeStatus,
    require_goal_transition,
)

logger = logging.getLogger(__name__)


class OrchestrationError(RuntimeError):
    pass


class FlowConflictError(OrchestrationError):
    """A stale writer tried to update a Flow with an outdated revision."""


class GoalTransitionError(OrchestrationError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


def _id() -> str:
    return uuid.uuid4().hex


class OrchestrationStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── Goals ───────────────────────────────────────────────────────────────

    def create_goal(
        self,
        objective: str,
        *,
        owner_id: str = "owner",
        session_id: str = "",
        acceptance_criteria: Sequence[str] | None = None,
        max_cycles: int = 3,
        budget: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
        workspace: str | Path | None = None,
        mutation_required: bool = False,
        test_command: Sequence[str] | None = None,
    ) -> Goal:
        canonical_workspace = ""
        if workspace is not None:
            from .autonomy import WorkspaceBoundary

            canonical_workspace = str(
                WorkspaceBoundary(Path(workspace), writable=mutation_required).validate()
            )
        with self._session() as session:
            goal = Goal(
                owner_id=owner_id,
                session_id=session_id,
                objective=objective,
                workspace=canonical_workspace,
                mutation_required=bool(mutation_required),
                test_command=[str(item) for item in (test_command or ["pytest", "-q"])],
                status=GoalState.PENDING.value,
                acceptance_criteria=list(acceptance_criteria or []),
                max_cycles=max(int(max_cycles), 1),
                budget=dict(budget or {}),
                meta=dict(meta or {}),
            )
            session.add(goal)
            session.flush()
            return goal

    def get_goal(self, goal_id: str) -> Goal | None:
        with self._session() as session:
            return session.get(Goal, goal_id)

    def list_goals(
        self, status: str | None = None, owner_id: str | None = None, limit: int = 100
    ) -> list[Goal]:
        with self._session() as session:
            stmt = select(Goal).order_by(Goal.created_at.desc())
            if status:
                stmt = stmt.where(Goal.status == status)
            if owner_id:
                stmt = stmt.where(Goal.owner_id == owner_id)
            return list(session.execute(stmt.limit(limit)).scalars().all())

    def list_active_goals(self, limit: int = 50) -> list[Goal]:
        """Goals the engine should work on right now."""
        with self._session() as session:
            return list(
                session.execute(
                    select(Goal)
                    .where(
                        Goal.status.in_(
                            [GoalState.PENDING.value, GoalState.ACTIVE.value,
                             GoalState.WAITING.value, GoalState.BLOCKED.value]
                        )
                    )
                    .order_by(Goal.created_at)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def transition_goal(
        self,
        goal_id: str,
        from_state: GoalState,
        to_state: GoalState,
        *,
        reason: str = "",
        result: Mapping[str, Any] | None = None,
        failure_reason: str | None = None,
    ) -> Goal:
        """CAS goal transition: only valid if current status == from_state."""
        try:
            require_goal_transition(from_state, to_state)
        except GoalStateError as exc:
            raise GoalTransitionError(str(exc)) from exc
        ts = _now()
        values: dict[str, Any] = {
            "status": to_state.value,
            "updated_at": ts,
        }
        if to_state == GoalState.ACTIVE and from_state == GoalState.PENDING:
            values["started_at"] = ts
        if to_state in GOAL_TERMINAL:
            values["finished_at"] = ts
        if result is not None:
            values["result"] = dict(result)
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        with self._session() as session:
            res = session.execute(
                update(Goal)
                .where(Goal.id == goal_id, Goal.status == from_state.value)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                raise GoalTransitionError(
                    f"goal {goal_id} is not in {from_state.value} (lost update)"
                )
            goal = session.get(Goal, goal_id)
            assert goal is not None
            return goal

    def bind_flow(self, goal_id: str, flow_id: str) -> None:
        with self._session() as session:
            session.execute(
                update(Goal)
                .where(Goal.id == goal_id)
                .values(current_flow_id=flow_id)
                .execution_options(synchronize_session=False)
            )

    def bump_cycle(self, goal_id: str) -> int:
        """Atomic cycle increment (SQL, not read-modify-write) — concurrent
        REPLANs can never lose an increment."""
        with self._session() as session:
            session.execute(
                update(Goal)
                .where(Goal.id == goal_id)
                .values(cycle_count=Goal.cycle_count + 1)
                .execution_options(synchronize_session=False)
            )
            goal = session.get(Goal, goal_id)
            if goal is None:
                raise OrchestrationError(f"goal not found: {goal_id}")
            return int(goal.cycle_count or 0)

    def update_meta(self, goal_id: str, **meta: Any) -> None:
        """Atomically merge JSON keys into goal.meta (SQL json_patch — no
        read-modify-write clobber under concurrent stall/evidence updates)."""
        with self._session() as session:
            patch = json.dumps({k: v for k, v in meta.items() if v is not None})
            try:
                session.execute(
                    text(
                        "UPDATE goals SET meta = json_patch(COALESCE(meta, '{}'), :patch) "
                        "WHERE id = :gid"
                    ),
                    {"patch": patch, "gid": goal_id},
                )
            except Exception:  # noqa: BLE001 — SQLite < 3.38 fallback
                goal = session.get(Goal, goal_id)
                if goal is not None:
                    goal.meta = dict(goal.meta or {}) | {
                        k: v for k, v in meta.items() if v is not None
                    }

    # ── Flows (optimistic revision CAS) ────────────────────────────────────

    def create_flow(
        self,
        goal_id: str,
        plan: Mapping[str, Any],
        state: Mapping[str, Any] | None = None,
        current_stage: str = "",
    ) -> GoalFlow:
        with self._session() as session:
            flow = GoalFlow(
                goal_id=goal_id,
                status=FlowState.PLANNED.value,
                revision=1,
                plan=dict(plan),
                state=dict(state or {}),
                current_stage=current_stage,
            )
            session.add(flow)
            session.flush()
            return flow

    def get_flow(self, flow_id: str) -> GoalFlow | None:
        with self._session() as session:
            return session.get(GoalFlow, flow_id)

    def get_current_flow(self, goal_id: str) -> GoalFlow | None:
        with self._session() as session:
            return (
                session.execute(
                    select(GoalFlow)
                    .where(GoalFlow.goal_id == goal_id)
                    .order_by(GoalFlow.created_at.desc())
                )
                .scalars()
                .first()
            )

    def update_flow(
        self,
        flow_id: str,
        expected_revision: int,
        *,
        status: FlowState | str | None = None,
        plan: Mapping[str, Any] | None = None,
        state: Mapping[str, Any] | None = None,
        current_stage: str | None = None,
    ) -> GoalFlow:
        """Optimistic-revision CAS update. Raises FlowConflictError if the
        expected revision is stale (a newer writer already advanced)."""
        values: dict[str, Any] = {
            "revision": int(expected_revision) + 1,
            "updated_at": _now(),
        }
        if status is not None:
            values["status"] = status.value if isinstance(status, FlowState) else status
        if plan is not None:
            values["plan"] = dict(plan)
        if state is not None:
            values["state"] = dict(state)
        if current_stage is not None:
            values["current_stage"] = current_stage
        with self._session() as session:
            res = session.execute(
                update(GoalFlow)
                .where(
                    GoalFlow.id == flow_id,
                    GoalFlow.revision == int(expected_revision),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if _rowcount(res) != 1:
                raise FlowConflictError(
                    f"flow {flow_id} revision conflict: expected {expected_revision}, "
                    f"stale writer rejected"
                )
            flow = session.get(GoalFlow, flow_id)
            assert flow is not None
            return flow

    def supersede_flow(self, flow_id: str, expected_revision: int) -> GoalFlow:
        return self.update_flow(
            flow_id, expected_revision, status=FlowState.SUPERSEDED
        )

    # ── Wake events ────────────────────────────────────────────────────────

    def enqueue_wake(
        self,
        kind: WakeKind | str,
        *,
        goal_id: str | None = None,
        flow_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> WakeEvent:
        with self._session() as session:
            ev = WakeEvent(
                goal_id=goal_id,
                flow_id=flow_id,
                kind=kind.value if isinstance(kind, WakeKind) else kind,
                payload=dict(payload or {}),
                status=WakeStatus.PENDING.value,
            )
            session.add(ev)
            session.flush()
            return ev

    def pending_wakes(self, limit: int = 50) -> list[WakeEvent]:
        with self._session() as session:
            return list(
                session.execute(
                    select(WakeEvent)
                    .where(WakeEvent.status == WakeStatus.PENDING.value)
                    .order_by(WakeEvent.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def mark_wake(self, wake_id: int, status: WakeStatus | str = WakeStatus.FIRED) -> None:
        with self._session() as session:
            session.execute(
                update(WakeEvent)
                .where(WakeEvent.id == wake_id)
                .values(
                    status=status.value if isinstance(status, WakeStatus) else status,
                    fired_at=_now(),
                )
                .execution_options(synchronize_session=False)
            )

    def has_pending_wake_for(self, goal_id: str, kind: WakeKind | str | None = None) -> bool:
        with self._session() as session:
            stmt = select(WakeEvent.id).where(
                WakeEvent.goal_id == goal_id,
                WakeEvent.status == WakeStatus.PENDING.value,
            )
            if kind is not None:
                stmt = stmt.where(
                    WakeEvent.kind == (kind.value if isinstance(kind, WakeKind) else kind)
                )
            return session.execute(stmt.limit(1)).first() is not None

    # ── Service health ─────────────────────────────────────────────────────

    def set_health(
        self,
        service_id: str,
        state: ServiceState | str = ServiceState.UNKNOWN,
        capacity: str | None = None,
        *,
        failure_class: str | None = None,
        probe_ok: bool | None = None,
        observed: Mapping[str, Any] | None = None,
    ) -> None:
        st = state.value if isinstance(state, ServiceState) else state
        with self._session() as session:
            row = session.get(ServiceHealthState, service_id)
            if row is None:
                row = ServiceHealthState(
                    service_id=service_id,
                    state=st,
                    capacity=capacity or "UNKNOWN",
                    observed=dict(observed or {}),
                )
                if probe_ok is not None:
                    row.last_probe_ok = probe_ok
                row.last_checked_at = _now()
                if failure_class:
                    # First failure of an unseen service counts too (it must
                    # be recorded, not silently dropped).
                    row.failure_count = 1
                    row.last_failure_class = failure_class
                    row.last_failure_at = _now()
                session.add(row)
            else:
                row.state = st
                if capacity:
                    row.capacity = capacity
                if probe_ok is not None:
                    row.last_probe_ok = probe_ok
                row.last_checked_at = _now()
                if failure_class:
                    row.failure_count = (row.failure_count or 0) + 1
                    row.last_failure_class = failure_class
                    row.last_failure_at = _now()
                if observed:
                    row.observed = dict(row.observed or {}) | dict(observed)
            session.flush()

    def reset_failures(self, service_id: str) -> None:
        """Clear failure history and mark AVAILABLE (called on success)."""
        with self._session() as session:
            row = session.get(ServiceHealthState, service_id)
            if row is None:
                row = ServiceHealthState(
                    service_id=service_id,
                    state=ServiceState.AVAILABLE.value,
                    last_probe_ok=True,
                )
                session.add(row)
            else:
                row.state = ServiceState.AVAILABLE.value
                row.failure_count = 0
                row.last_failure_class = None
                row.last_failure_at = None
                row.last_probe_ok = True
                row.last_checked_at = _now()
            session.flush()

    def get_health(self, service_id: str) -> ServiceHealthState | None:
        with self._session() as session:
            return session.get(ServiceHealthState, service_id)

    def all_health(self) -> dict[str, ServiceHealthState]:
        with self._session() as session:
            return {
                h.service_id: h
                for h in session.execute(select(ServiceHealthState)).scalars().all()
            }

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            sid: {
                "state": h.state,
                "capacity": h.capacity,
                "failure_count": h.failure_count or 0,
                "last_failure_class": h.last_failure_class,
                "last_probe_ok": h.last_probe_ok,
            }
            for sid, h in self.all_health().items()
        }

    # ── Handoffs ───────────────────────────────────────────────────────────

    def create_handoff(
        self,
        *,
        original_worker: str,
        replacement_worker: str,
        reason_for_handoff: str,
        goal_id: str | None = None,
        flow_id: str | None = None,
        task_id: str | None = None,
        handoff_state: Mapping[str, Any] | None = None,
    ) -> ServiceHandoff:
        with self._session() as session:
            h = ServiceHandoff(
                goal_id=goal_id,
                flow_id=flow_id,
                task_id=task_id,
                original_worker=original_worker,
                replacement_worker=replacement_worker,
                reason_for_handoff=reason_for_handoff,
                handoff_state=dict(handoff_state or {}),
            )
            session.add(h)
            session.flush()
            return h

    def complete_handoff(self, handoff_id: str, result: Mapping[str, Any]) -> None:
        with self._session() as session:
            session.execute(
                update(ServiceHandoff)
                .where(ServiceHandoff.id == handoff_id)
                .values(completed_at=_now(), result=dict(result))
                .execution_options(synchronize_session=False)
            )

    def list_handoffs(self, goal_id: str | None = None, limit: int = 50) -> list[ServiceHandoff]:
        with self._session() as session:
            stmt = select(ServiceHandoff).order_by(ServiceHandoff.created_at.desc())
            if goal_id:
                stmt = stmt.where(ServiceHandoff.goal_id == goal_id)
            return list(session.execute(stmt.limit(limit)).scalars().all())


# ── Timer helpers for WAIT ──────────────────────────────────────────────────


def wait_until(seconds: int) -> datetime:
    return _now() + timedelta(seconds=max(int(seconds), 1))


def is_timer_expired(goal: Goal, now: datetime | None = None) -> bool:
    """A WAITING goal resumes when its timer (meta['wake_at']) expires."""
    ts = now or _now()
    wake_at = (goal.meta or {}).get("wake_at")
    if not wake_at:
        return False
    try:
        return ts >= datetime.fromisoformat(str(wake_at))
    except ValueError:
        return False
