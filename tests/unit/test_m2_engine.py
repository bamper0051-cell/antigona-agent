"""M2 Goal Engine — mandatory E2E scenarios (E2E-1..8) with controlled fakes.

Every scenario runs the REAL GoalEngine + M1 kernel + real SQLite; only the
service CLI adapters are replaced by a scripted fake (controlled failover).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from antigona.database import Database
from antigona.kernel import KernelStore, TaskState
from antigona.orchestration import (
    GoalState,
    OrchestrationStore,
    ServiceState,
    WakeKind,
)
from antigona.orchestration.engine import GoalEngine
from antigona.orchestration.executors import ExecResult
from antigona.orchestration.router import ROLE_TO_SERVICES, ServiceRouter
from antigona.orchestration.state import JudgeDecision
from antigona.orchestration.wake import WakeManager
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore


class FakeExecutors:
    """Scripted service pool: fail_script maps service -> failure_class."""

    def __init__(self, fail_script: dict[str, str] | None = None,
                 fail_verification: bool = False) -> None:
        self.fail_script = fail_script or {}
        self.fail_verification = fail_verification
        self.calls: list[str] = []

    def execute(
        self, service: str, instruction: str, *, workspace: str | None = None,
        writable: bool = False,
    ) -> ExecResult:
        self.calls.append(service)
        fc = self.fail_script.get(service)
        if fc:
            return ExecResult(ok=False, output="", failure_class=fc,
                              duration_s=0.01, service_id=service)
        if self.fail_verification and "Stage: verification" in instruction:
            return ExecResult(ok=False, output="verify failed",
                              failure_class="TASK_FAILURE",
                              duration_s=0.01, service_id=service)
        assert workspace is not None
        if "Stage: analysis" in instruction:
            from antigona.orchestration.autonomy import hash_file, snapshot_workspace

            root = Path(workspace)
            snapshot = snapshot_workspace(root)
            output = json.dumps({
                "kind": "analysis", "snapshot_hash": snapshot.tree_hash,
                "summary": "fixture inspected",
                "evidence": [{"path": "input.txt", "sha256": hash_file(root / "input.txt"),
                              "line": 1}],
            })
        elif "Stage: implementation" in instruction and writable:
            (Path(workspace) / "app.py").write_text("VALUE = 2\n")
            output = json.dumps({"kind": "implementation", "summary": "fixed value"})
        elif "Stage: implementation" in instruction:
            from antigona.orchestration.autonomy import snapshot_workspace

            output = json.dumps({
                "kind": "result", "input_hash": snapshot_workspace(Path(workspace)).tree_hash,
                "result": {"ok": True}, "derivation": "deterministic fixture result",
            })
        elif "Frozen candidate evidence: " in instruction:
            candidate = json.loads(instruction.split("Frozen candidate evidence: ", 1)[1])
            marker = "criteria="
            criteria = json.loads(instruction.rsplit(marker, 1)[1].splitlines()[0])
            output = json.dumps({
                "kind": "verification", "candidate_hash": candidate["candidate_hash"],
                "criteria": [{"criterion": item, "passed": True, "evidence": "checked"}
                             for item in criteria],
            })
        else:
            result_hash = hashlib.sha256(b'{"ok":true}').hexdigest()
            from antigona.orchestration.autonomy import snapshot_workspace

            marker = "criteria="
            criteria = json.loads(instruction.rsplit(marker, 1)[1].splitlines()[0])
            output = json.dumps({
                "kind": "result_verification",
                "input_hash": snapshot_workspace(Path(workspace)).tree_hash,
                "producer_result_hash": result_hash, "passed": True,
                "evidence": "independently recomputed fixture result",
                "criteria": [{"criterion": item, "passed": True, "evidence": "checked"}
                             for item in criteria],
            })
        return ExecResult(ok=True, output=output,
                          failure_class="", duration_s=0.01, service_id=service)

    def simulate(self, service: str, failure_class: str) -> ExecResult:
        return self.execute(service, "simulate")


def make_env(tmp_path: Path, fail_script: dict[str, str] | None = None,
             fail_verification: bool = False):
    db = Database(f"sqlite:///{tmp_path / 'e.sqlite'}")
    db.create_all()
    ks = KernelStore(db.session_factory)
    orch = OrchestrationStore(db.session_factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("fixture\n")
    (workspace / "app.py").write_text("VALUE = 1\n")
    (workspace / "test_app.py").write_text(
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n"
    )
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    original_create_goal = orch.create_goal

    def create_goal(objective: str, **kwargs: Any):
        kwargs.setdefault("workspace", workspace)
        return original_create_goal(objective, **kwargs)

    orch.create_goal = create_goal  # type: ignore[method-assign]
    grants = ApprovalGrantStore(db_path=str(tmp_path / "g.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)
    fake = FakeExecutors(fail_script, fail_verification)
    engine = GoalEngine(
        ks, orch, policy_engine=policy, grant_store=grants,
        executors=fake, poll_interval=0.01, wait_retry_seconds=1,
    )
    return db, ks, orch, fake, engine


async def drive_to_terminal(engine: GoalEngine, goal_id: str, max_ticks: int = 60) -> None:
    for _ in range(max_ticks):
        await engine.tick()
        g = engine.orch.get_goal(goal_id)
        if g is not None and g.status in (
            GoalState.SUCCEEDED.value, GoalState.FAILED.value, GoalState.CANCELLED.value
        ):
            return
        await asyncio.sleep(0.01)


# ── E2E-1: autonomous continuation (verification fails -> REPLAN -> DONE) ───


@pytest.mark.asyncio
async def test_e2e1_autonomous_continuation_no_owner_prompt(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path, fail_verification=True)
    g = orch.create_goal(
        "Fix problem X completely",
        acceptance_criteria=["tests pass", "review clean"],
        max_cycles=6,
    )
    # Cycle 0..: verification keeps failing across all services -> automatic
    # REPLAN. Let it replan at least twice (still ACTIVE), WITHOUT owner input.
    for _ in range(40):
        await engine.tick()
        g2 = orch.get_goal(g.id)
        if g2 is not None and g2.cycle_count >= 2 and g2.status == GoalState.ACTIVE.value:
            break
    g2 = orch.get_goal(g.id)
    assert g2.status == GoalState.ACTIVE.value, f"got {g2.status}"
    assert g2.cycle_count >= 2  # REPLAN happened automatically (no owner prompt)
    # Services heal; the goal continues on its own to verified DONE.
    fake.fail_verification = False
    await drive_to_terminal(engine, g.id, max_ticks=60)
    g = orch.get_goal(g.id)
    assert g.status == GoalState.SUCCEEDED.value, f"got {g.status}"
    assert g.cycle_count >= 2  # completed on a later cycle
    # Evidence present in the result.
    assert g.result and g.result["evidence"]["verification_evidence"]


# ── E2E-2: restart recovers an ACTIVE goal ──────────────────────────────────


@pytest.mark.asyncio
async def test_e2e2_restart_recovers_active_goal(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Restart test", acceptance_criteria=["done"])
    await engine.tick()  # plan + start execution
    assert orch.get_goal(g.id).status in (GoalState.ACTIVE.value, GoalState.SUCCEEDED.value)
    # Simulate process restart: brand-new engine on the same durable DB.
    db2 = Database(f"sqlite:///{tmp_path / 'e.sqlite'}")
    ks2 = KernelStore(db2.session_factory)
    orch2 = OrchestrationStore(db2.session_factory)
    grants2 = ApprovalGrantStore(db_path=str(tmp_path / "g.sqlite"))
    policy2 = PolicyEngine(require_approval=True, grant_store=grants2)
    fake2 = FakeExecutors()
    engine2 = GoalEngine(ks2, orch2, policy_engine=policy2, grant_store=grants2,
                         executors=fake2, poll_interval=0.01)
    await drive_to_terminal(engine2, g.id)
    assert orch2.get_goal(g.id).status == GoalState.SUCCEEDED.value


# ── E2E-3: WAIT/WAKE — durable wait, released worker, WakeEvent resumes ─────


@pytest.mark.asyncio
async def test_e2e3_wait_wake_resumes_goal(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Wait test", acceptance_criteria=["done"])
    # Activate first, then put the goal durably into WAIT (worker released,
    # timer-only: no wake event enqueued yet).
    orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)
    WakeManager(orch).wait(g.id, reason="awaiting owner decision",
                           wake_at_iso=None)
    assert orch.get_goal(g.id).status == GoalState.WAITING.value
    # No wake yet -> engine keeps it parked (no tokens burned).
    await engine.tick()
    assert orch.get_goal(g.id).status == GoalState.WAITING.value
    # Owner message arrives -> WakeEvent -> resume -> goal completes.
    orch.enqueue_wake(WakeKind.OWNER_MESSAGE, goal_id=g.id, payload={"go": True})
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value


@pytest.mark.asyncio
async def test_e2e3_timer_wait_resumes_after_expiry(tmp_path):
    from datetime import UTC, datetime, timedelta

    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Timer wait", acceptance_criteria=["done"])
    await engine.tick()  # plan + start
    # Park the goal durably with a timer that has ALREADY expired.
    WakeManager(orch).wait(
        g.id, reason="waiting on external process",
        wake_at_iso=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert orch.get_goal(g.id).status == GoalState.WAITING.value
    # A tick sees the expired timer and resumes automatically (no owner input).
    await drive_to_terminal(engine, g.id, max_ticks=40)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value


# ── E2E-4: service rate limit -> handoff persisted -> replacement continues ─


@pytest.mark.asyncio
async def test_e2e4_rate_limit_failover_handoff(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path, fail_script={"claude": "RATE_LIMIT"})
    g = orch.create_goal("Failover test", acceptance_criteria=["done"])
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value
    handoffs = orch.list_handoffs(goal_id=g.id)
    assert len(handoffs) >= 1
    assert handoffs[0].original_worker == "claude"
    assert handoffs[0].replacement_worker == "codex"
    assert "RATE_LIMIT" in handoffs[0].reason_for_handoff
    # claude is now marked RATE_LIMITED in health.
    assert orch.get_health("claude").state == ServiceState.RATE_LIMITED.value


# ── E2E-5: multiple service failure -> third worker completes ───────────────


@pytest.mark.asyncio
async def test_e2e5_multiple_failures_third_worker(tmp_path):
    db, ks, orch, fake, engine = make_env(
        tmp_path,
        fail_script={"claude": "RATE_LIMIT", "codex": "QUOTA_EXHAUSTED"},
    )
    g = orch.create_goal("Triple failover", acceptance_criteria=["done"], max_cycles=3)
    # Cycle 0: claude + codex fail -> agy (third worker) implements. Strict
    # role separation then blocks same-service verification, so the goal must
    # NOT reach DONE while only one service is healthy.
    impl_ok = False
    for _ in range(10):
        await engine.tick()
        flow = orch.get_current_flow(g.id)
        if flow:
            for tid in flow.plan["task_ids"]:
                t = ks.get_task(str(tid))
                if t and (t.payload or {}).get("stage") == "implementation":
                    if t.status == TaskState.SUCCEEDED.value:
                        impl_ok = True
                        break
            if impl_ok:
                break
    assert impl_ok, "implementation must have succeeded via the third worker"
    assert "agy" in fake.calls  # the third worker executed the work
    flow = orch.get_current_flow(g.id)
    impl_task = None
    for tid in flow.plan["task_ids"]:
        t = ks.get_task(str(tid))
        if t and (t.payload or {}).get("stage") == "implementation":
            impl_task = t
    assert impl_task is not None and impl_task.status == TaskState.SUCCEEDED.value
    assert (impl_task.result or {}).get("service") == "agy"
    assert len(orch.list_handoffs(goal_id=g.id)) >= 2
    # The failed services recover (health reset); verification then runs on a
    # DIFFERENT service than the implementer -> independent DONE.
    fake.fail_script = {}
    orch.set_health("claude", ServiceState.AVAILABLE, "HIGH")
    orch.set_health("codex", ServiceState.AVAILABLE, "MEDIUM")
    await drive_to_terminal(engine, g.id, max_ticks=60)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value


# ── E2E-6: implementer != final independent reviewer ────────────────────────


def test_e2e6_independent_roles(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path)
    r = ServiceRouter(orch)
    implementer = r.choose_for_role("implementer")
    reviewer = r.choose_for_role("reviewer")
    auditor = r.choose_for_role("auditor")
    assert implementer != reviewer, "implementer and reviewer must differ"
    assert auditor == "grok" and "grok" not in ROLE_TO_SERVICES["implementer"]
    assert "grok" not in ROLE_TO_SERVICES["reviewer"]


# ── E2E-7: capacity routing at engine level ─────────────────────────────────


@pytest.mark.asyncio
async def test_e2e7_capacity_routing_engine_level(tmp_path):
    db, ks, orch, fake, engine = make_env(tmp_path)
    orch.set_health("claude", ServiceState.AVAILABLE, "LOW")
    orch.set_health("codex", ServiceState.AVAILABLE, "HIGH")
    orch.set_health("agy", ServiceState.AVAILABLE, "HIGH")
    g = orch.create_goal("Capacity", acceptance_criteria=["done"])
    orch.update_meta(g.id, size="large")  # planner puts size into task payloads
    # Give the first task a large-size hint via goal meta (planner reads it).
    await engine.tick()
    await engine.tick()  # allow dispatcher to execute
    assert fake.calls, "a service must have been called"
    assert fake.calls[0] in ("codex", "agy"), f"large task must not hit LOW capacity, got {fake.calls[0]}"


# ── E2E-8: stale result fenced (flow revision CAS + M1 run fencing) ─────────


def test_e2e8_stale_flow_update_rejected(tmp_path):
    from antigona.orchestration import FlowConflictError

    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Stale", acceptance_criteria=["done"])
    asyncio.run(engine.tick())
    flow = orch.get_current_flow(g.id)
    assert flow is not None
    rev = flow.revision
    orch.update_flow(flow.id, rev, current_stage="next")
    # A stale writer with the OLD revision must be rejected (its result never
    # overwrites newer truth).
    with pytest.raises(FlowConflictError):
        orch.update_flow(flow.id, rev, current_stage="stale-result")
    fresh = orch.get_flow(flow.id)
    assert fresh.current_stage == "next"


def test_e2e8_stale_run_finalize_fenced(tmp_path):
    """A stale service result is fenced at the M1 run level: after a run is
    reclaimed (LOST), the old worker's finalize raises KernelFenceError.
    (Same guarantee as the M1 regression, exercised through M2's stack.)"""
    from datetime import UTC, datetime, timedelta

    from antigona.kernel import RunState
    from antigona.kernel.store import KernelFenceError

    db, ks, orch, fake, engine = make_env(tmp_path)
    # A fresh goal task in the kernel (not yet executed).
    task = ks.create_task(owner_id="o", kind="goal_task",
                          payload={"stage": "x"}, max_attempts=2)
    ks.claim_task(task.id, "wA", lease_seconds=5)
    run = ks.create_run(task.id, "wA", lease_seconds=5)
    # Reclaimer steals the expired run; the stale worker can no longer finalize.
    ks.reconcile(worker_id="wB", lease_seconds=5,
                 now=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(KernelFenceError):
        ks.finalize_run(run.id, "wA", RunState.SUCCEEDED)


# ── Adversarial tests (Grok audit round 1 findings) ─────────────────────────


@pytest.mark.asyncio
async def test_stub_verification_never_mints_done(tmp_path):
    """Grok CRITICAL: a stub (hermes) auto-success must never mint DONE.
    All real services fail -> the goal must NOT succeed on fake evidence."""
    db, ks, orch, fake, engine = make_env(tmp_path, fail_script={
        "claude": "TIMEOUT", "codex": "TIMEOUT", "agy": "TIMEOUT",
    })
    g = orch.create_goal("No stub DONE", acceptance_criteria=["x"], max_cycles=1)
    await drive_to_terminal(engine, g.id, max_ticks=30)
    g2 = orch.get_goal(g.id)
    # Never SUCCEEDED; the flow must not be marked SUCCEEDED either.
    assert g2.status != GoalState.SUCCEEDED.value
    flow = orch.get_current_flow(g.id)
    if flow is not None:
        assert flow.status != "SUCCEEDED"
    # hermes was never selected as an implementer.
    assert "hermes" not in fake.calls


@pytest.mark.asyncio
async def test_e2e6_independent_roles_at_runtime(tmp_path):
    """Grok MAJOR: E2E-6 must hold at EXECUTION, not just map level:
    the verification task of a succeeded goal ran on a DIFFERENT service
    than the implementation task."""
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Role separation", acceptance_criteria=["ok"], max_cycles=2)
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value
    flow = orch.get_current_flow(g.id)
    services = {}
    for tid in flow.plan["task_ids"]:
        t = ks.get_task(str(tid))
        stage = (t.payload or {}).get("stage")
        result = t.result or {}
        if t.status == TaskState.SUCCEEDED.value:
            services[stage] = result.get("service")
    assert services.get("verification"), "verification must have run"
    assert services.get("implementation"), "implementation must have run"
    assert services["verification"] != services["implementation"], (
        f"verifier {services['verification']} must differ from implementer "
        f"{services['implementation']}"
    )


def test_waiting_goal_can_finish_terminal(tmp_path):
    """Grok Crash/Recovery MAJOR: a parked (WAITING) goal whose work resolved
    must be allowed to transition straight to SUCCEEDED/FAILED."""
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("Parked finish")
    orch.transition_goal(g.id, GoalState.PENDING, GoalState.ACTIVE)
    orch.transition_goal(g.id, GoalState.ACTIVE, GoalState.WAITING)
    g2 = orch.transition_goal(g.id, GoalState.WAITING, GoalState.SUCCEEDED,
                              result={"ok": True})
    assert g2.status == GoalState.SUCCEEDED.value


# ── Grok round-2 findings: stage DAG + handoff task_id assertions ──────────


@pytest.mark.asyncio
async def test_stage_dag_blocks_verification_until_implementation(tmp_path):
    """Grok round-2 MAJOR: verification must be race-safe — it stays BLOCKED
    until implementation SUCCEEDED (kernel dependency graph), so it can never
    overtake the implementer and self-verify."""
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal("DAG order", acceptance_criteria=["ok"])
    await engine.tick()  # plan only (tasks created; dispatcher runs after)
    flow = orch.get_current_flow(g.id)
    assert flow.state["workspace"] == g.workspace
    assert flow.plan["workspace"] == g.workspace
    states = {}
    for tid in flow.plan["task_ids"]:
        t = ks.get_task(str(tid))
        assert (t.payload or {})["workspace"] == g.workspace
        states[(t.payload or {}).get("stage")] = t.status
    # At plan time verification must be BLOCKED by the DAG (unless the fake
    # already executed everything — tick() ends with a dispatcher pass).
    assert states["verification"] in (TaskState.BLOCKED.value, TaskState.SUCCEEDED.value)
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value
    # Evidence: verifier != implementer, both recorded.
    flow = orch.get_current_flow(g.id)
    services = {}
    for tid in flow.plan["task_ids"]:
        t = ks.get_task(str(tid))
        if t.status == TaskState.SUCCEEDED.value:
            services[(t.payload or {}).get("stage")] = (t.result or {}).get("service")
    assert services["verification"] != services["implementation"]


@pytest.mark.asyncio
async def test_handoff_persists_task_id(tmp_path):
    """Grok round-2 MINOR: handoff rows must carry a non-empty task_id."""
    db, ks, orch, fake, engine = make_env(tmp_path, fail_script={"claude": "RATE_LIMIT"})
    g = orch.create_goal("Handoff task id", acceptance_criteria=["done"])
    await drive_to_terminal(engine, g.id)
    handoffs = orch.list_handoffs(goal_id=g.id)
    assert handoffs, "a handoff must exist"
    assert handoffs[0].task_id, "handoff.task_id must be non-empty"


@pytest.mark.asyncio
async def test_judge_rejects_sparse_verifier_first_style_evidence(tmp_path: Path) -> None:
    """A green task set plus different labels and prose is not autonomy-valid DONE."""
    db, _, orch, _, engine = make_env(tmp_path)
    goal = orch.create_goal("Sparse evidence", acceptance_criteria=["done"])
    await drive_to_terminal(engine, goal.id)
    flow = orch.get_current_flow(goal.id)
    assert flow is not None
    current = orch.get_goal(goal.id)
    sparse_meta = dict(current.meta or {})
    sparse_meta["verification_evidence"] = {
            "flow_id": flow.id,
            "service": "codex",
            "output": "all good",
    }
    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE goals SET meta = ? WHERE id = ?", (json.dumps(sparse_meta), goal.id)
        )
    verdict = engine.judge.decide(orch.get_goal(goal.id), orch.get_flow(flow.id))
    assert verdict.decision is not JudgeDecision.DONE


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason='mutation tests run inside WorkspaceBoundary (bubblewrap), unavailable on Windows (Wave 4)')
async def test_goal_engine_mutation_required_positive_e2e(tmp_path: Path) -> None:
    db, ks, orch, fake, engine = make_env(tmp_path)
    goal = orch.create_goal(
        "Fix the value", acceptance_criteria=["tests pass"], mutation_required=True,
        test_command=["pytest", "-q"],
    )
    await drive_to_terminal(engine, goal.id, max_ticks=60)
    completed = orch.get_goal(goal.id)
    assert completed.status == GoalState.SUCCEEDED.value
    evidence = (completed.result or {})["evidence"]["verification_evidence"]
    assert evidence["implementation_run_id"]
    assert evidence["candidate_timestamp"]
    assert evidence["test_command"] == ["pytest", "-q"]
    assert evidence["test_exit_code"] == 0


@pytest.mark.asyncio
async def test_real_failing_pytest_prevents_goal_done(tmp_path: Path) -> None:
    db, _, orch, _, engine = make_env(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "test_app.py").write_text(
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 999\n"
    )
    goal = orch.create_goal(
        "Fix the value", acceptance_criteria=["tests pass"], mutation_required=True,
        test_command=["pytest", "-q"], max_cycles=1,
    )
    await drive_to_terminal(engine, goal.id, max_ticks=60)
    completed = orch.get_goal(goal.id)
    assert completed.status != GoalState.SUCCEEDED.value
    flow = orch.get_current_flow(goal.id)
    if flow is not None:
        assert flow.status != "SUCCEEDED"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason='mutation tests run inside WorkspaceBoundary (bubblewrap), unavailable on Windows (Wave 4)')
async def test_judge_rejects_stale_candidate_and_mismatched_flow_done(
    tmp_path: Path,
) -> None:
    db, _, orch, _, engine = make_env(tmp_path)
    goal = orch.create_goal(
        "Fix the value", acceptance_criteria=["tests pass"], mutation_required=True,
        test_command=["pytest", "-q"],
    )
    await drive_to_terminal(engine, goal.id, max_ticks=60)
    current = orch.get_goal(goal.id)
    flow = orch.get_current_flow(goal.id)
    assert flow is not None
    valid_meta = dict(current.meta or {})

    stale = dict(valid_meta["verification_evidence"])
    stale["candidate_hash"] = "f" * 64
    orch.update_meta(goal.id, verification_evidence=stale)
    verdict = engine.judge.decide(orch.get_goal(goal.id), flow)
    assert verdict.decision is not JudgeDecision.DONE

    mismatched = dict(valid_meta["verification_evidence"])
    mismatched["flow_id"] = "flow-from-another-cycle"
    orch.update_meta(goal.id, verification_evidence=mismatched)
    verdict = engine.judge.decide(orch.get_goal(goal.id), flow)
    assert verdict.decision is not JudgeDecision.DONE


@pytest.mark.asyncio
async def test_result_only_producer_evidence_is_bound_to_current_flow_and_run(
    tmp_path: Path,
) -> None:
    """A valid result-only completion names its exact durable producer attempt."""
    _, ks, orch, _, engine = make_env(tmp_path)
    goal = orch.create_goal("Compute result", acceptance_criteria=["result is correct"])
    await drive_to_terminal(engine, goal.id)

    completed = orch.get_goal(goal.id)
    flow = orch.get_current_flow(goal.id)
    assert completed is not None and flow is not None
    assert completed.status == GoalState.SUCCEEDED.value
    producer = dict((completed.meta or {})["producer_evidence"])
    assert producer["flow_id"] == flow.id
    assert producer["goal_id"] == goal.id
    assert producer["producer_task_id"] in flow.plan["task_ids"]
    assert producer["producer_run_id"]
    assert producer["producer_service"]
    assert producer["goal_hash"] == hashlib.sha256(goal.objective.encode()).hexdigest()
    assert producer["workspace_hash"] == producer["input_hash"]
    assert datetime.fromisoformat(producer["produced_at"]).tzinfo is not None
    run = ks.get_run(producer["producer_run_id"])
    assert run is not None and run.task_id == producer["producer_task_id"]


@pytest.mark.asyncio
async def test_judge_rejects_flow_a_producer_with_flow_b_verification(
    tmp_path: Path,
) -> None:
    """Executable Reviewer-B attack: producer A + verifier B cannot mint DONE."""
    _, _, orch, _, engine = make_env(tmp_path)
    goal = orch.create_goal("Compute result", acceptance_criteria=["result is correct"])
    await drive_to_terminal(engine, goal.id)
    flow_a = orch.get_current_flow(goal.id)
    completed_a = orch.get_goal(goal.id)
    assert flow_a is not None and completed_a is not None
    producer_a = dict((completed_a.meta or {})["producer_evidence"])
    verification_b = dict((completed_a.meta or {})["verification_evidence"])

    flow_b = orch.create_flow(
        goal.id,
        plan=dict(flow_a.plan or {}),
        state={"goal_id": goal.id, "workspace": goal.workspace},
        current_stage="verification",
    )
    orch.update_flow(
        flow_b.id,
        expected_revision=flow_b.revision,
        status="SUCCEEDED",
    )
    orch.bind_flow(goal.id, flow_b.id)
    verification_b["flow_id"] = flow_b.id
    verification_b["verified_at"] = datetime.now(UTC).isoformat()
    orch.update_meta(
        goal.id,
        producer_evidence=producer_a,
        verification_evidence=verification_b,
    )

    verdict = engine.judge.decide(orch.get_goal(goal.id), orch.get_flow(flow_b.id))
    assert producer_a.get("flow_id") != flow_b.id
    assert verdict.decision is not JudgeDecision.DONE


@pytest.mark.asyncio
async def test_judge_rejects_malformed_or_stale_result_producer_variants(
    tmp_path: Path,
) -> None:
    db, ks, orch, _, engine = make_env(tmp_path)
    goal = orch.create_goal("Compute result", acceptance_criteria=["result is correct"])
    await drive_to_terminal(engine, goal.id)
    completed = orch.get_goal(goal.id)
    flow = orch.get_current_flow(goal.id)
    assert completed is not None and flow is not None
    valid_meta = dict(completed.meta or {})
    valid_producer = dict(valid_meta["producer_evidence"])
    assert engine.judge.decide(completed, flow).decision is JudgeDecision.DONE

    implementation_task = ks.get_task(valid_producer["producer_task_id"])
    assert implementation_task is not None
    other_task_id = next(
        str(task_id)
        for task_id in flow.plan["task_ids"]
        if str(task_id) != implementation_task.id
    )
    other_runs = ks.list_runs(other_task_id)
    assert other_runs
    variants: list[dict[str, object]] = []
    missing_flow = dict(valid_producer)
    missing_flow.pop("flow_id")
    variants.append(missing_flow)
    variants.append({**valid_producer, "flow_id": "wrong-flow"})
    variants.append({**valid_producer, "producer_task_id": other_task_id})
    variants.append({**valid_producer, "producer_run_id": other_runs[-1].id})
    variants.append({**valid_producer, "produced_at": "not-a-timestamp"})

    for producer in variants:
        variant_meta = {**valid_meta, "producer_evidence": producer}
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE goals SET meta = ? WHERE id = ?",
                (json.dumps(variant_meta), goal.id),
            )
        verdict = engine.judge.decide(orch.get_goal(goal.id), flow)
        assert verdict.decision is not JudgeDecision.DONE

    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE goals SET meta = ? WHERE id = ?",
            (json.dumps(valid_meta), goal.id),
        )
    assert engine.judge.decide(orch.get_goal(goal.id), flow).decision is JudgeDecision.DONE


# ── Variant A: deterministic verifier (no LLM) ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason='mutation tests run inside WorkspaceBoundary (bubblewrap), unavailable on Windows (Wave 4)')
async def test_deterministic_verifier_passes_with_passing_test(tmp_path):
    """Variant A: with a test_command present, verification runs the command on
    the frozen candidate (no LLM). Exit 0 => every criterion passed => SUCCEEDED,
    and the verification task records service 'deterministic'."""
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal(
        "Deterministic verify", acceptance_criteria=["ok"], mutation_required=True,
        test_command=["/bin/true"], max_cycles=2,
    )
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status == GoalState.SUCCEEDED.value
    flow = orch.get_current_flow(g.id)
    verif_evidence = None
    for tid in flow.plan["task_ids"]:
        t = ks.get_task(str(tid))
        if (t.payload or {}).get("stage") == "verification":
            verif_evidence = (t.result or {}).get("evidence") or {}
    assert verif_evidence, "verification task must have run"
    assert verif_evidence.get("verifier_service") == "deterministic", (
        f"verification must be deterministic, got "
        f"{verif_evidence.get('verifier_service')}"
    )


@pytest.mark.asyncio
async def test_deterministic_verifier_fails_on_failing_test(tmp_path):
    """Variant A: a failing test_command must fail the goal (never self-verify)."""
    db, ks, orch, fake, engine = make_env(tmp_path)
    g = orch.create_goal(
        "Deterministic verify fail", acceptance_criteria=["ok"], mutation_required=True,
        test_command=["/bin/false"], max_cycles=2,
    )
    await drive_to_terminal(engine, g.id)
    assert orch.get_goal(g.id).status != GoalState.SUCCEEDED.value
