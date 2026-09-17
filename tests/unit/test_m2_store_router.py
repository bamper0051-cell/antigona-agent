"""M2 unit tests — durable Goal/Flow store, wake, health, handoff, router."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from antigona.database import Database
from antigona.orchestration import (
    Capacity,
    FailureClass,
    FlowConflictError,
    GoalState,
    OrchestrationStore,
    ServiceState,
    WakeKind,
)
from antigona.orchestration.router import ROLE_TO_SERVICES, ServiceRouter


def make_env(tmp_path: Path):
    db = Database(f"sqlite:///{tmp_path / 'm2.sqlite'}")
    db.create_all()
    orch = OrchestrationStore(db.session_factory)
    return orch, db


def test_goal_autonomy_column_migration_is_replay_safe(tmp_path: Path) -> None:
    target = tmp_path / "legacy.sqlite"
    with sqlite3.connect(target) as connection:
        connection.execute(
            "CREATE TABLE goals (id VARCHAR(64) PRIMARY KEY, objective TEXT NOT NULL)"
        )
    first = Database(f"sqlite:///{target}")
    first.create_all()
    first.create_all()
    with sqlite3.connect(target) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(goals)")}
    assert {"workspace", "mutation_required", "test_command"} <= columns


# ── Goal CAS transitions ────────────────────────────────────────────────────


def test_goal_lifecycle_cas(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X", acceptance_criteria=["a"], max_cycles=2)
    assert g.status == GoalState.PENDING.value
    g = orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)
    assert g.status == GoalState.ACTIVE.value and g.started_at is not None
    g = orch.transition_goal(g.id, GoalState.ACTIVE, GoalState.SUCCEEDED,
                             result={"ok": True})
    assert g.status == GoalState.SUCCEEDED.value and g.finished_at is not None
    assert g.result == {"ok": True}


def test_goal_lost_update_rejected(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)
    from antigona.orchestration.store import GoalTransitionError

    with pytest.raises(GoalTransitionError):
        orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)  # stale from
    with pytest.raises(GoalTransitionError):
        orch.transition_goal(g.id, GoalState.ACTIVE, GoalState.PENDING)  # invalid


def test_goal_concurrent_transition_one_winner(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    outcomes: list[str] = []
    lock = threading.Lock()

    def race(i: int) -> None:
        try:
            orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)
            with lock:
                outcomes.append(f"w{i}")
        except Exception:
            pass

    ths = [threading.Thread(target=race, args=(i,)) for i in range(16)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(outcomes) == 1, f"expected exactly 1 winner, got {len(outcomes)}"
    assert orch.get_goal(g.id).status == GoalState.ACTIVE.value


# ── Flow optimistic revision CAS (E2E-8 primitive) ──────────────────────────


def test_flow_revision_cas_stale_writer_rejected(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    f = orch.create_flow(g.id, {"stages": ["a"]})
    assert f.revision == 1
    f2 = orch.update_flow(f.id, 1, current_stage="a")
    assert f2.revision == 2
    # A stale writer using revision 1 must be rejected (E2E-8).
    with pytest.raises(FlowConflictError):
        orch.update_flow(f.id, 1, current_stage="stale")
    # Fresh revision works.
    f3 = orch.update_flow(f.id, 2, current_stage="b")
    assert f3.revision == 3 and f3.current_stage == "b"


def test_flow_supersede_keeps_history(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    f1 = orch.create_flow(g.id, {"s": ["a"]})
    f2 = orch.create_flow(g.id, {"s": ["b"]})
    orch.supersede_flow(f1.id, f1.revision)
    assert orch.get_flow(f1.id).status == "SUPERSEDED"
    assert orch.get_current_flow(g.id).id == f2.id  # newest flow is active


# ── Wake queue ──────────────────────────────────────────────────────────────


def test_wake_enqueue_pending_fire(tmp_path):
    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    ev = orch.enqueue_wake(WakeKind.TASK_COMPLETED, goal_id=g.id, payload={"t": "1"})
    assert orch.has_pending_wake_for(g.id, WakeKind.TASK_COMPLETED)
    pending = orch.pending_wakes()
    assert len(pending) == 1 and pending[0].id == ev.id
    orch.mark_wake(ev.id, "FIRED")
    assert not orch.has_pending_wake_for(g.id, WakeKind.TASK_COMPLETED)


# ── Service health + handoff ────────────────────────────────────────────────


def test_health_and_handoff(tmp_path):
    orch, _ = make_env(tmp_path)
    orch.set_health("claude", ServiceState.AVAILABLE, Capacity.HIGH.value)
    assert orch.get_health("claude").state == ServiceState.AVAILABLE.value
    orch.set_health("claude", ServiceState.DEGRADED, failure_class="TIMEOUT")
    h = orch.get_health("claude")
    assert h.state == "DEGRADED" and h.failure_count == 1
    hand = orch.create_handoff(
        original_worker="claude", replacement_worker="codex",
        reason_for_handoff="RATE_LIMIT", goal_id="g1",
        handoff_state={"objective": "x"},
    )
    orch.complete_handoff(hand.id, {"ok": True})
    assert orch.list_handoffs(goal_id="g1")[0].result == {"ok": True}


# ── Service Router (capacity-aware, E2E-7) ──────────────────────────────────


def test_router_default_and_roles(tmp_path):
    orch, _ = make_env(tmp_path)
    r = ServiceRouter(orch)
    assert r.route({}) == "claude"
    assert r.choose_for_role("reviewer") == "codex"
    assert r.choose_for_role("auditor") == "grok"


def test_router_capacity_routing_e2e7(tmp_path):
    """E2E-7: Service A capacity LOW, Service B HIGH, large task -> router
    chooses B (capacity-aware, not just availability)."""
    orch, _ = make_env(tmp_path)
    orch.set_health("claude", ServiceState.AVAILABLE, Capacity.LOW.value)
    orch.set_health("codex", ServiceState.AVAILABLE, Capacity.HIGH.value)
    orch.set_health("agy", ServiceState.AVAILABLE, Capacity.HIGH.value)
    r = ServiceRouter(orch)
    chosen = r.route({}, role="implementer", size="large")
    assert chosen in ("codex", "agy"), f"large task must go to HIGH capacity, got {chosen}"
    # Small task may still use the LOW-capacity service (save high capacity).
    small = r.route({}, role="implementer", size="small")
    assert small == "claude"


def test_router_failover_skips_unavailable(tmp_path):
    orch, _ = make_env(tmp_path)
    r = ServiceRouter(orch)
    r.record_failure("claude", FailureClass.RATE_LIMIT)
    r.record_failure("codex", FailureClass.QUOTA_EXHAUSTED)
    assert orch.get_health("claude").state == ServiceState.RATE_LIMITED.value
    assert orch.get_health("codex").state == ServiceState.RATE_LIMITED.value
    chosen = r.route({})
    assert chosen == "agy", f"rate-limited services must be skipped, got {chosen}"
    r.record_success("agy")
    assert orch.get_health("agy").state == ServiceState.AVAILABLE.value


def test_router_service_hint_boost(tmp_path):
    orch, _ = make_env(tmp_path)
    r = ServiceRouter(orch)
    chosen = r.route({"service_hint": "agy"})
    assert chosen == "agy"


def test_mutation_required_routes_around_incapable_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, _ = make_env(tmp_path)
    router = ServiceRouter(orch)
    monkeypatch.setitem(ROLE_TO_SERVICES, "implementer", ["grok", "codex"])
    payload = {"stage": "implementation", "mutation_required": True}
    assert router.route(payload, role="implementer") == "codex"
    assert router.route(payload, role="implementer", avoid={"codex"}) is None


# ── Timer helpers ───────────────────────────────────────────────────────────


def test_timer_expiry(tmp_path):
    from datetime import UTC, datetime, timedelta

    from antigona.orchestration.store import is_timer_expired, wait_until

    orch, _ = make_env(tmp_path)
    g = orch.create_goal("Fix X")
    orch.update_meta(g.id, wake_at=wait_until(60).isoformat())
    assert is_timer_expired(orch.get_goal(g.id)) is False
    orch.update_meta(g.id, wake_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    assert is_timer_expired(orch.get_goal(g.id)) is True


# ── Router: review-driven fixes (Claude review of Codex router) ────────────


def test_router_unknown_role_never_selects_grok(tmp_path):
    orch, _ = make_env(tmp_path)
    r = ServiceRouter(orch)
    chosen = r.choose_for_role("mystery-role")
    assert chosen in ("claude", "codex", "agy", "hermes")
    assert chosen != "grok"


def test_router_cooldown_readmits_rate_limited_service(tmp_path):
    """Corollary of E2E-4: after the cooldown window a RATE_LIMITED service
    may re-enter the pool (transient rate limits must not ban it forever)."""
    from datetime import UTC, datetime, timedelta

    from antigona.orchestration.models import ServiceHealthState

    orch, db = make_env(tmp_path)
    r = ServiceRouter(orch)
    orch.set_health("claude", ServiceState.RATE_LIMITED, "HIGH",
                    failure_class=FailureClass.RATE_LIMIT.value)
    assert r.choose_for_role("implementer") == "codex"
    # Make the healthy replacements busy, force the failed service back after
    # its cooldown window has passed.
    orch.set_health("codex", ServiceState.BUSY, "MEDIUM")
    orch.set_health("agy", ServiceState.BUSY, "HIGH")
    orch.set_health("hermes", ServiceState.BUSY, "HIGH")
    with db.session_factory() as s:
        row = s.get(ServiceHealthState, "claude")
        row.last_failure_at = datetime.now(UTC) - timedelta(seconds=301)
        s.commit()
    assert r.choose_for_role("implementer") == "claude"


def test_router_first_failure_of_unseen_service_counts(tmp_path):
    orch, _ = make_env(tmp_path)
    orch.set_health("claude", ServiceState.DEGRADED, "HIGH",
                    failure_class=FailureClass.TIMEOUT.value)
    h = orch.get_health("claude")
    assert h is not None and h.failure_count == 1
    assert h.last_failure_class == FailureClass.TIMEOUT.value
