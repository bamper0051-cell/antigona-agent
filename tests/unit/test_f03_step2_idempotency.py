from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from antigona.durable.tool_ledger import DurableToolLedger
from antigona.engine.unified_executor import (
    ToolExecutionRequest,
    UnifiedToolExecutionLayer,
    compute_call_hash,
    is_read_only_tool,
)


@pytest.fixture
def fake_ledger(tmp_path: Any) -> DurableToolLedger:
    return DurableToolLedger(db_path=tmp_path / "test_ledger.db")


@pytest.fixture
def fake_policy_engine() -> MagicMock:
    pe = MagicMock()

    async def _check(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"allowed": True}

    pe.check = _check
    return pe


@pytest.fixture
def fake_registry() -> MagicMock:
    registry = MagicMock()
    # Mock dispatch to return JSON success
    async def _dispatch(tool_name: str, **kwargs: Any) -> str:
        return json.dumps({"success": True, "tool": tool_name, "args": kwargs})

    registry.dispatch = _dispatch
    return registry


@pytest.mark.asyncio
async def test_case1_single_logical_call_executes_once(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )
    req = ToolExecutionRequest(
        tool_name="count_tokens",
        params={"text": "hello"},
        requester="llm",
        turn_id="turn-1",
    )
    res = await layer.execute(req)
    data = json.loads(res)
    assert data["success"] is True


@pytest.mark.asyncio
async def test_case2_same_turn_repeated_side_effect_not_repeated(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    call_count = 0

    async def _side_effect_dispatch(tool_name: str, **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        return json.dumps({"success": True, "call_count": call_count})

    fake_registry.dispatch = _side_effect_dispatch
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )

    req = ToolExecutionRequest(
        tool_name="write_file",
        params={"path": "/tmp/a.txt", "content": "data"},
        requester="llm",
        turn_id="turn-repeat-1",
    )

    res1 = await layer.execute(req)
    res2 = await layer.execute(req)

    assert res1 == res2
    assert call_count == 1  # Executed exactly once!


@pytest.mark.asyncio
async def test_case3_restart_returns_durable_result(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )
    turn_id = "turn-restart-1"
    tool_name = "memorize"
    params = {"store": "memory", "content": "fact"}

    ch = compute_call_hash(turn_id, tool_name, params)

    req = ToolExecutionRequest(
        tool_name=tool_name,
        params=params,
        requester="llm",
        turn_id=turn_id,
    )
    res1 = await layer.execute(req)

    # Pre-populate ledger (simulating persisted state across restart)
    layer._ledger[ch] = {"status": "COMPLETED", "result": res1}

    res2 = await layer.execute(req)
    assert res1 == res2


@pytest.mark.asyncio
async def test_case4_duplicate_delivery_prevents_duplicate_side_effect(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    executed: list[str] = []

    async def _dispatch(tool_name: str, **kwargs: Any) -> str:
        executed.append(tool_name)
        return json.dumps({"ok": True})

    fake_registry.dispatch = _dispatch
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )

    req = ToolExecutionRequest(
        tool_name="generate_image",
        params={"prompt": "cat"},
        requester="llm",
        turn_id="turn-telegram-dup",
    )

    await layer.execute(req)
    await layer.execute(req)

    assert len(executed) == 1


@pytest.mark.asyncio
async def test_case5_parallel_duplicate_calls_race_condition(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    call_counter = 0

    async def _slow_dispatch(tool_name: str, **kwargs: Any) -> str:
        nonlocal call_counter
        call_counter += 1
        await asyncio.sleep(0.05)
        return json.dumps({"counter": call_counter})

    fake_registry.dispatch = _slow_dispatch
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )

    req = ToolExecutionRequest(
        tool_name="write_file",
        params={"path": "/tmp/parallel.txt", "content": "p"},
        requester="llm",
        turn_id="turn-parallel-1",
    )

    res1, res2 = await asyncio.gather(layer.execute(req), layer.execute(req))
    assert call_counter == 1
    assert res1 == res2


@pytest.mark.asyncio
async def test_case6_unknown_non_idempotent_execution_fails_closed(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )
    turn_id = "turn-unknown-1"
    tool_name = "write_file"
    params = {"path": "/tmp/unknown.txt", "content": "x"}
    ch = compute_call_hash(turn_id, tool_name, params)

    # Manually simulate a crashed/unknown pending state in durable ledger
    layer.durable_ledger.reserve(ch, turn_id, tool_name)
    layer.durable_ledger.settle(ch, "EXECUTION_UNKNOWN")


    req = ToolExecutionRequest(
        tool_name=tool_name,
        params=params,
        requester="llm",
        turn_id=turn_id,
    )

    res = await layer.execute(req)
    data = json.loads(res)
    assert data.get("execution_unknown") is True
    assert "EXECUTION_UNKNOWN" in data.get("error", "")


@pytest.mark.asyncio
async def test_case7_explicit_read_only_call_reexecution_allowed(
    fake_policy_engine: MagicMock, fake_registry: MagicMock, fake_ledger: DurableToolLedger
) -> None:
    counts = 0

    async def _dispatch(tool_name: str, **kwargs: Any) -> str:
        nonlocal counts
        counts += 1
        return json.dumps({"count": counts})

    fake_registry.dispatch = _dispatch
    layer = UnifiedToolExecutionLayer(
        policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=fake_ledger
    )

    # READ_ONLY tool count_tokens
    req = ToolExecutionRequest(
        tool_name="count_tokens",
        params={"text": "hello"},
        requester="llm",
        turn_id="turn-readonly-1",
    )
    ch = compute_call_hash("turn-readonly-1", "count_tokens", {"text": "hello"})
    assert is_read_only_tool("count_tokens", {"text": "hello"}) is True

    # First call completes
    await layer.execute(req)

    # If cleared or pending, read-only is safe to execute again
    layer._ledger.pop(ch, None)
    layer.durable_ledger.clear(ch)
    res2 = await layer.execute(req)
    assert json.loads(res2)["count"] == 2



def test_case8_different_args_yields_different_call_hash() -> None:
    ch1 = compute_call_hash("turn-1", "write_file", {"path": "/tmp/a.txt", "content": "1"})
    ch2 = compute_call_hash("turn-1", "write_file", {"path": "/tmp/a.txt", "content": "2"})
    assert ch1 != ch2


def test_case9_same_args_different_json_key_order_yields_same_call_hash() -> None:
    ch1 = compute_call_hash("turn-1", "write_file", {"path": "/tmp/a.txt", "content": "hello"})
    ch2 = compute_call_hash("turn-1", "write_file", {"content": "hello", "path": "/tmp/a.txt"})
    assert ch1 == ch2


@pytest.mark.asyncio
async def test_sqlite_durability_across_process_restarts(tmp_path: Any, fake_policy_engine: MagicMock, fake_registry: MagicMock) -> None:
    db_file = tmp_path / "test_ledger.db"
    from antigona.durable.tool_ledger import DurableToolLedger

    ledger1 = DurableToolLedger(db_path=db_file)
    layer1 = UnifiedToolExecutionLayer(policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=ledger1)

    req = ToolExecutionRequest(
        tool_name="write_file",
        params={"path": "/tmp/durable.txt", "content": "persist"},
        requester="llm",
        turn_id="turn-restart-db",
    )

    res1 = await layer1.execute(req)

    # Instantiate a NEW layer simulating process restart reading the same DB
    ledger2 = DurableToolLedger(db_path=db_file)
    layer2 = UnifiedToolExecutionLayer(policy_engine=fake_policy_engine, registry=fake_registry, durable_ledger=ledger2)

    res2 = await layer2.execute(req)

    assert res1 == res2



def test_call_ordinal_and_tool_call_id_slot_differentiation() -> None:
    # Retries of the same call share ordinal/ID
    ch1 = compute_call_hash("turn-1", "write_file", {"p": "1"}, call_ordinal=1)
    ch2 = compute_call_hash("turn-1", "write_file", {"p": "1"}, call_ordinal=1)
    assert ch1 == ch2

    # Two identical intentional calls in the same turn have different ordinals or tool_call_ids
    ch3 = compute_call_hash("turn-1", "write_file", {"p": "1"}, call_ordinal=2)
    assert ch1 != ch3

    ch_id1 = compute_call_hash("turn-1", "write_file", {"p": "1"}, tool_call_id="call_abc")
    ch_id2 = compute_call_hash("turn-1", "write_file", {"p": "1"}, tool_call_id="call_def")
    assert ch_id1 != ch_id2

