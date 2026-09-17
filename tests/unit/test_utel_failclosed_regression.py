"""Wave 3 and Wave 4A UTEL Audit — ToolRegistry & ActionExecutor Fail-Closed Regression Guard.

Ensures that:
1. ActionExecutor.execute(Action(type=ActionType.RUN_SHELL)) fails closed
   (DENIED BEFORE process execution) when invoked without owner authorization, grant, or PIN.
2. ToolRegistry.dispatch("run_shell") fails closed without valid approval grant:
   (a) unauthorized / no grant -> deny, zero process calls;
   (b) policy requires approval even if tool metadata accidentally false -> deny;
   (c) valid grant wiring with real grant store fixture executes safely under mock;
   (d) command binding mismatch and replay must deny;
   (e) normal non-shell registered tool regression is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from antigona.security.approval_grant import ApprovalGrantStore
from antigona.tools.action_executor import Action, ActionExecutor, ActionType
from antigona.tools.registry import ToolRegistry, register_builtins


@pytest.mark.asyncio
async def test_action_executor_run_shell_fails_closed_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """ActionExecutor RUN_SHELL must fail closed without calling subprocess when unauthenticated."""
    # Ensure clean unauthenticated environment (no owner ID, no CLI owner, no PIN)
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    monkeypatch.delenv("ANTIGONA_OWNER_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGONA_CLI_OWNER_USER", raising=False)
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)

    # Sentinel command string
    sentinel_cmd = "UTEL_PROBE_NONEXECUTED"
    invoked_processes: list[str] = []

    # Mock all subprocess creation surfaces to ensure no real execution occurs
    mock_async_proc = AsyncMock()
    mock_async_proc.returncode = 0
    mock_async_proc.communicate.return_value = (b"", b"")

    async def _trap_async_subprocess(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"asyncio.create_subprocess_shell: {args}")
        return mock_async_proc

    def _trap_subprocess_run(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"subprocess.run: {args}")
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _trap_async_subprocess)
    monkeypatch.setattr(subprocess, "run", _trap_subprocess_run)

    executor = ActionExecutor()
    action = Action(type=ActionType.RUN_SHELL, command=sentinel_cmd)

    # Execute action as an unauthenticated actor
    result = await executor._async_execute(
        action,
        channel="cli",
        user_id="unauthorized_user_12345",
        session_id="unauth_session",
    )

    # Assert execution was denied fail-closed before any subprocess mock was called
    assert result.success is False
    assert result.error == "ACCESS_DENIED"
    assert "Доступ запрещён" in result.message
    assert invoked_processes == []


@pytest.mark.asyncio
async def test_tool_registry_run_shell_unauthorized_no_grant_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(a) ToolRegistry.dispatch('run_shell') without grant must deny and make zero process calls."""
    invoked_processes: list[str] = []

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"sentinel_output", b"")

    async def _trap_async_subprocess(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"asyncio.create_subprocess_shell: {args}")
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _trap_async_subprocess)

    registry = ToolRegistry()
    register_builtins(registry)
    store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    registry.grant_store = store

    # Verify run_shell is explicitly registered with requires_approval=True
    tool = registry.get("run_shell")
    assert tool.requires_approval is True

    # Dispatch run_shell without grant token
    raw = await registry.dispatch(
        "run_shell",
        command="echo UTEL_SENTINEL_PROBE",
        _user_id="unauthorized_actor",
        _channel="cli",
    )
    result = json.loads(raw)

    assert result.get("requires_approval") is True
    assert "error" in result
    assert invoked_processes == [], "Subprocess must not be called when grant is missing"


@pytest.mark.asyncio
async def test_tool_registry_policy_requires_approval_enforced_even_if_tool_metadata_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(b) Policy requires_approval=True must be enforced even if tool descriptor has requires_approval=False."""
    invoked_processes: list[str] = []

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")

    async def _trap_async_subprocess(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"asyncio.create_subprocess_shell: {args}")
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _trap_async_subprocess)

    class _PolicyRequiringApproval:
        async def check(self, **kwargs: Any) -> dict[str, Any]:
            # Returns allowed=True but requires_approval=True
            return {
                "allowed": True,
                "requires_approval": True,
                "reason": "Policy requires approval verification",
            }

    handler_calls: list[dict[str, Any]] = []

    async def custom_shell_handler(**kwargs: Any) -> str:
        handler_calls.append(kwargs)
        proc = await asyncio.create_subprocess_shell(kwargs.get("command", ""))
        await proc.communicate()
        return json.dumps({"success": True})

    registry = ToolRegistry()
    # Explicitly register with requires_approval=False to test policy override
    registry.register(
        "mock_shell",
        toolset="shell",
        schema={"type": "object", "properties": {"command": {"type": "string"}}},
        handler=custom_shell_handler,
        requires_approval=False,
        replace=True,
    )
    registry.policy_engine = _PolicyRequiringApproval()
    registry.grant_store = ApprovalGrantStore(tmp_path / "grants.sqlite")

    raw = await registry.dispatch(
        "mock_shell",
        command="echo test",
        _user_id="actor_1",
    )
    result = json.loads(raw)

    assert result.get("requires_approval") is True
    assert result.get("error") == "Policy requires approval verification"
    assert handler_calls == []
    assert invoked_processes == []


@pytest.mark.asyncio
async def test_tool_registry_run_shell_with_valid_grant_executes_safely_mocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(c) Valid one-shot grant wiring executes safely with mocked asyncio.create_subprocess_shell."""
    invoked_processes: list[str] = []

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"output_success_data", b"")

    async def _trap_async_subprocess(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"asyncio.create_subprocess_shell: {args}")
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _trap_async_subprocess)

    registry = ToolRegistry()
    register_builtins(registry)
    store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    registry.grant_store = store

    sentinel_cmd = "echo HELLO_SAFE_MOCK"
    token = store.issue(
        actor="authorized_owner",
        tool_name="run_shell",
        args={"command": sentinel_cmd},
        issuer="owner_pin_verification",
    )

    raw = await registry.dispatch(
        "run_shell",
        command=sentinel_cmd,
        _user_id="authorized_owner",
        _channel="cli",
        approval_token=token,
    )
    result = json.loads(raw)

    assert result.get("success") is True
    assert result.get("output") == "output_success_data"
    assert len(invoked_processes) == 1
    assert sentinel_cmd in invoked_processes[0]


@pytest.mark.asyncio
async def test_tool_registry_run_shell_command_mismatch_and_replay_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(d) Command binding mismatch and replay of one-shot grant must deny with zero unauthorized calls."""
    invoked_processes: list[str] = []

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")

    async def _trap_async_subprocess(*args: Any, **kwargs: Any) -> Any:
        invoked_processes.append(f"asyncio.create_subprocess_shell: {args}")
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _trap_async_subprocess)

    registry = ToolRegistry()
    register_builtins(registry)
    store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    registry.grant_store = store

    approved_cmd = "echo approved_cmd"
    tampered_cmd = "echo tampered_cmd"

    token = store.issue(
        actor="owner",
        tool_name="run_shell",
        args={"command": approved_cmd},
        issuer="owner_approval",
    )

    # 1. Tampered command dispatch with approved token -> mismatch -> DENY
    mismatch_raw = await registry.dispatch(
        "run_shell",
        command=tampered_cmd,
        _user_id="owner",
        approval_token=token,
    )
    mismatch_res = json.loads(mismatch_raw)
    assert mismatch_res.get("requires_approval") is True
    assert invoked_processes == []

    # 2. Approved command dispatch with valid token -> SUCCESS (1 execution)
    success_raw = await registry.dispatch(
        "run_shell",
        command=approved_cmd,
        _user_id="owner",
        approval_token=token,
    )
    success_res = json.loads(success_raw)
    assert success_res.get("success") is True
    assert len(invoked_processes) == 1

    # 3. Replay of consumed token with same approved command -> DENY
    replay_raw = await registry.dispatch(
        "run_shell",
        command=approved_cmd,
        _user_id="owner",
        approval_token=token,
    )
    replay_res = json.loads(replay_raw)
    assert replay_res.get("requires_approval") is True
    # Still only 1 execution occurred; replay did NOT call subprocess
    assert len(invoked_processes) == 1


@pytest.mark.asyncio
async def test_tool_registry_normal_non_shell_tool_unaffected(tmp_path: Path) -> None:
    """(e) Normal non-shell registered tools without approval requirements execute without regression."""
    from antigona.core import paths

    ws_dir = paths.workspace_dir()
    ws_dir.mkdir(parents=True, exist_ok=True)
    test_file = ws_dir / "sample_test_doc.txt"
    test_file.write_text("hello regression guard", encoding="utf-8")

    registry = ToolRegistry()
    register_builtins(registry)
    store = ApprovalGrantStore(tmp_path / "grants.sqlite")
    registry.grant_store = store

    # read_file is a SAFE non-shell tool that runs without requiring approval
    raw = await registry.dispatch(
        "read_file",
        path="sample_test_doc.txt",
        _user_id="owner",
    )
    result = json.loads(raw)
    assert result.get("success") is True
    assert "hello regression guard" in result.get("content", "")
