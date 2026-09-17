from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from antigona.database import Base
from antigona.kernel import KernelStore
from antigona.orchestration import (
    ExecResult,
    GoalEngine,
    GoalState,
    OrchestrationStore,
    ServiceExecutors,
)
from antigona.orchestration.autonomy import hash_file, snapshot_workspace


class MockExecutors(ServiceExecutors):
    """Scripted service pool matching the current strict contract: returns valid
    stage JSON per stage and honours the workspace/writable kwargs the handler
    passes. `fail_first_stage` makes the first 3 calls fail (REPLAN path)."""

    def __init__(self, fail_first_stage: bool = False) -> None:
        super().__init__()
        self.fail_first_stage = fail_first_stage
        self.call_count = 0

    def execute(
        self,
        service: str,
        instruction: str,
        *,
        workspace: str | None = None,
        writable: bool = False,
    ) -> ExecResult:
        self.call_count += 1
        if self.fail_first_stage and self.call_count <= 3:
            return ExecResult(
                ok=False,
                output="simulated failure on first task execution",
                failure_class="TASK_FAILURE",
                duration_s=0.01,
                service_id=service,
            )
        assert workspace is not None
        root = Path(workspace)
        if "Stage: analysis" in instruction:
            snap = snapshot_workspace(root)
            output = json.dumps({
                "kind": "analysis", "snapshot_hash": snap.tree_hash,
                "summary": "fixture inspected",
                "evidence": [{"path": "input.txt",
                              "sha256": hash_file(root / "input.txt"), "line": 1}],
            })
        elif "Stage: implementation" in instruction and writable:
            (root / "app.py").write_text("VALUE = 2\n")
            output = json.dumps({"kind": "implementation", "summary": "fixed value"})
        elif "Stage: implementation" in instruction:
            output = json.dumps({
                "kind": "result", "input_hash": snapshot_workspace(root).tree_hash,
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
            marker = "criteria="
            criteria = json.loads(instruction.rsplit(marker, 1)[1].splitlines()[0])
            output = json.dumps({
                "kind": "result_verification",
                "input_hash": snapshot_workspace(root).tree_hash,
                "producer_result_hash": result_hash, "passed": True,
                "evidence": "independently recomputed fixture result",
                "criteria": [{"criterion": item, "passed": True, "evidence": "checked"}
                             for item in criteria],
            })
        return ExecResult(ok=True, output=output, failure_class="",
                          duration_s=0.01, service_id=service)


@pytest.fixture
def session_factory(tmp_path: Path):
    db_path = tmp_path / "m2_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "input.txt").write_text("fixture\n")
    (ws / "app.py").write_text("VALUE = 1\n")
    (ws / "test_app.py").write_text(
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n"
    )
    (ws / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return ws


@pytest.mark.asyncio
async def test_m2_goal_pipeline_e2e_happy_path(session_factory, tmp_path: Path):
    kernel_store = KernelStore(session_factory)
    orch_store = OrchestrationStore(session_factory)
    ws = _workspace(tmp_path)

    goal_engine = GoalEngine(
        kernel_store=kernel_store,
        orch=orch_store,
        executors=MockExecutors(),
        poll_interval=0.05,
    )

    goal = orch_store.create_goal(
        objective="E2E test: verify goal pipeline end to end",
        owner_id="e2e_owner",
        acceptance_criteria=[
            "tasks executed successfully",
            "verification evidence recorded",
            "goal result persisted",
        ],
        workspace=str(ws),
    )
    goal_id = goal.id
    assert goal.status == GoalState.PENDING.value

    # Tick 1: PENDING -> ACTIVE, flow planned + 3 kernel tasks.
    await goal_engine.tick()
    goal = orch_store.get_goal(goal_id)
    assert goal.status == GoalState.ACTIVE.value
    assert goal.current_flow_id is not None

    flow = orch_store.get_current_flow(goal_id)
    assert flow is not None
    assert len(flow.plan["task_ids"]) == 3

    for _ in range(10):
        await goal_engine.tick()
        goal = orch_store.get_goal(goal_id)
        if goal.status == GoalState.SUCCEEDED.value:
            break

    assert goal.status == GoalState.SUCCEEDED.value
    assert goal.result is not None
    assert "evidence" in goal.result
    assert goal.result["evidence"]["flow_status"] == "SUCCEEDED"
    assert goal.result["evidence"]["tasks_succeeded"] == 3


@pytest.mark.asyncio
async def test_m2_goal_pipeline_replan_recovery(session_factory, tmp_path: Path):
    kernel_store = KernelStore(session_factory)
    orch_store = OrchestrationStore(session_factory)
    ws = _workspace(tmp_path)

    goal_engine = GoalEngine(
        kernel_store=kernel_store,
        orch=orch_store,
        executors=MockExecutors(fail_first_stage=True),
        poll_interval=0.05,
    )

    goal = orch_store.create_goal(
        objective="E2E test: replan on failure",
        owner_id="e2e_owner",
        acceptance_criteria=["tasks executed successfully"],
        max_cycles=3,
        workspace=str(ws),
    )
    goal_id = goal.id

    for _ in range(15):
        await goal_engine.tick()
        goal = orch_store.get_goal(goal_id)
        if goal.status in (GoalState.SUCCEEDED.value, GoalState.FAILED.value):
            break

    # After 3 failed attempts, cycle budget or task failure leads to a terminal
    # state — never an infinite loop.
    assert goal.status in (GoalState.SUCCEEDED.value, GoalState.FAILED.value)
