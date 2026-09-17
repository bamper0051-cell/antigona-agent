"""Constitution P1-002 compatibility path: approval is enforced at execution."""
from __future__ import annotations

from pathlib import Path

import pytest

from antigona.database import Database
from antigona.kernel import KernelExecutor, KernelStore
from antigona.policy.engine import PolicyEngine
from antigona.security.approval_grant import ApprovalGrantStore

ACTION = "run_shell"
PARAMS = {"tool_name": ACTION, "command": "rm /tmp/constitutional-target"}


def _execution_stack(tmp_path: Path):
    db = Database(f"sqlite:///{tmp_path / 'kernel.sqlite'}")
    db.create_all()
    store = KernelStore(db.session_factory)
    grants = ApprovalGrantStore(db_path=str(tmp_path / "grants.sqlite"))
    policy = PolicyEngine(require_approval=True, grant_store=grants)
    effects: list[dict] = []

    async def handler(payload: dict, context: dict) -> dict:
        effects.append({"payload": payload, "context": context})
        return {"output": "executed"}

    return store, grants, KernelExecutor(policy_engine=policy, grant_store=grants, handler=handler), effects


@pytest.mark.asyncio
async def test_requires_approval_blocks_call_site_without_side_effect(tmp_path: Path) -> None:
    store, _grants, executor, effects = _execution_stack(tmp_path)
    task = store.create_task(owner_id="owner", kind=ACTION, payload=dict(PARAMS))

    outcome = await executor.execute_run(task=task, actor="owner")

    assert outcome["success"] is False
    assert outcome["outcome"] == "APPROVAL_REQUIRED"
    assert effects == []


@pytest.mark.asyncio
async def test_one_shot_grant_allows_once_then_blocks_reuse(tmp_path: Path) -> None:
    store, grants, executor, effects = _execution_stack(tmp_path)
    task = store.create_task(owner_id="owner", kind=ACTION, payload=dict(PARAMS))
    token = grants.issue(actor="owner", tool_name=ACTION, args=dict(PARAMS), issuer="test")

    task.payload = dict(task.payload) | {"approval_token": token}
    first = await executor.execute_run(task=task, actor="owner")
    assert first["success"] is True
    assert first["outcome"] == "SUCCEEDED"
    assert len(effects) == 1

    task.payload = dict(task.payload) | {"approval_token": token}
    replay = await executor.execute_run(task=task, actor="owner")
    assert replay["success"] is False
    assert replay["outcome"] == "APPROVAL_REQUIRED"
    assert len(effects) == 1
