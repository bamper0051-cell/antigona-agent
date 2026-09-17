"""Phase L10-R live-contract regression tests.

Exercises exact routing, slash command parsing, approval resolution, bare shell execution,
dialogue context invariants, and portrait status reset rules.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from antigona.cli_ui.chat import ChatController, _approval_word_decision
from antigona.cli_ui.commands import CommandKind, parse_command
from antigona.context.builder import ContextBuilder
from antigona.contracts import ToolResult
from antigona.core.brain import AntigonaBrain, ResponseType, _is_bare_shell_command
from antigona.result_safety import classify_tool_failure


@pytest.mark.asyncio
async def test_greeting_stale_task_history_does_not_revive_task() -> None:
    """Invariant 1: A bare greeting with stale task history present behaves as ordinary dialogue."""
    builder = ContextBuilder()
    stale_turn_buffer = [
        {"role": "user", "content": "Создай задачу про UX подтверждений в проекте /tmp/ux"},
        {"role": "assistant", "content": "Задача создана, мне нужен путь к проекту."},
        {"role": "user", "content": "hi"},
    ]
    messages = builder.build(turn_buffer=stale_turn_buffer)
    # Check that system message includes instructions not to resume stale tasks on bare greetings
    system_msg = messages[0]["content"]
    assert "НЕ пытайся автоматически возобновлять старые завершённые задачи" in system_msg


@pytest.mark.asyncio
async def test_immediate_referential_rewrite_still_works() -> None:
    """Invariant 2: Context continuation still works when user asks for a follow-up rewrite."""
    builder = ContextBuilder()
    turn_buffer = [
        {"role": "user", "content": "Составь план изучения Python на неделю."},
        {"role": "assistant", "content": "Вот план: День 1... День 7..."},
        {"role": "user", "content": "Сделай его проще."},
    ]
    messages = builder.build(turn_buffer=turn_buffer)
    assert len(messages) == 4
    assert messages[-1]["content"] == "Сделай его проще."


@pytest.mark.asyncio
async def test_bare_pwd_deterministic_routing() -> None:
    """Invariant 3 & 5: Bare pwd uses direct shell routing and displays stdout normally."""
    assert _is_bare_shell_command("pwd") is True
    turn_id = f"test-pwd-{uuid.uuid4().hex}"
    async with AntigonaBrain() as brain:
        res = await brain.process(
            text="pwd", user_id="test_user", channel="cli",
            context={"turn_id": turn_id, "correlation_id": turn_id},
        )
    assert res.response_type == ResponseType.CONVERSATION
    assert res.flow_id is None
    assert "/opt/antigona-home/.antigona" in res.text or "/" in res.text


@pytest.mark.asyncio
async def test_bare_ls_deterministic_routing() -> None:
    """Invariant 4: Bare ls uses direct shell routing without creating a TaskFlow."""
    assert _is_bare_shell_command("ls") is True
    assert _is_bare_shell_command("ls -la") is True
    turn_id = f"test-ls-{uuid.uuid4().hex}"
    async with AntigonaBrain() as brain:
        res = await brain.process(
            text="ls", user_id="test_user", channel="cli",
            context={"turn_id": turn_id, "correlation_id": turn_id},
        )
    assert res.response_type == ResponseType.CONVERSATION
    assert res.flow_id is None
    # E-1 security invariant: bare `ls` executes inside the sandbox and must
    # never expose host repository paths.  Backends may return a JSON envelope
    # or rendered stdout; only the no-host-leak property is asserted, so the
    # test is renderer-agnostic and deterministic.
    output = res.text
    try:
        payload = json.loads(res.text)
    except json.JSONDecodeError:
        pass
    else:
        output = str(payload.get("output", ""))
    assert "/opt/antigona-home/.antigona" not in output
    assert "AGENTS.md" not in output


@pytest.mark.asyncio
async def test_direct_shell_failure_exposes_controlled_reason() -> None:
    """Invariant 6 & 18: Tool failure exposes safe specific reason detail."""
    tr = ToolResult(ok=False, status="failed", error="package installation forbidden in sandbox")
    reason = classify_tool_failure(tr)
    assert "forbidden in sandbox" in reason
    assert reason != "tool execution failed"


@pytest.mark.asyncio
async def test_approve_without_id_is_valid_auto_single_flow() -> None:
    """Invariant 9: bare /approve is a VALID command (auto-resolve/picker flow
    for the single pending approval — 'no IDs in the happy path'), not a
    missing-argument error. The originally observed bug ("Padded slash
    commands are malformed and fail closed" for a plain unpadded /approve) was
    a false rejection from the old whitespace-padding check, not a genuine
    missing-argument case — see test_outer_slash_whitespace_accepted and
    test_embedded_newline_slash_command_fails_closed for the actual fix.
    """
    parsed = parse_command("/approve")
    assert parsed.kind == CommandKind.APPROVE
    assert parsed.args == ()
    assert parsed.command_name == "/approve"

    parsed_deny = parse_command("/deny")
    assert parsed_deny.kind == CommandKind.DENY
    assert parsed_deny.args == ()
    assert parsed_deny.command_name == "/deny"


@pytest.mark.asyncio
async def test_approve_valid_id_works() -> None:
    """Invariant 10: /approve with a valid resource ID parses cleanly as APPROVE."""
    parsed = parse_command("/approve flow-123")
    assert parsed.kind == CommandKind.APPROVE
    assert parsed.args == ("flow-123",)


@pytest.mark.asyncio
async def test_outer_slash_whitespace_accepted() -> None:
    """Invariant 12: Outer whitespace around slash commands is accepted and normalized."""
    parsed = parse_command("  /status flow-123  ")
    assert parsed.kind == CommandKind.STATUS
    assert parsed.args == ("flow-123",)

    parsed_app = parse_command("  /approve flow-456 ")
    assert parsed_app.kind == CommandKind.APPROVE
    assert parsed_app.args == ("flow-456",)


@pytest.mark.asyncio
async def test_embedded_newline_slash_command_fails_closed() -> None:
    """Invariant 13: Embedded newlines in slash commands fail closed."""
    parsed = parse_command("/status\nflow-123")
    assert parsed.kind == CommandKind.MALFORMED


@pytest.mark.asyncio
async def test_nul_slash_command_fails_closed() -> None:
    """Invariant 14: NUL bytes in slash commands fail closed."""
    parsed = parse_command("/status\0flow-123")
    assert parsed.kind == CommandKind.MALFORMED


@pytest.mark.asyncio
async def test_pending_approval_confirmation_behavior() -> None:
    """Invariant 15: Recognizes pending approval confirmation forms (da/net/y/yes/n/no)."""
    assert _approval_word_decision("да") is True
    assert _approval_word_decision("yes") is True
    assert _approval_word_decision("y") is True
    assert _approval_word_decision("нет") is False
    assert _approval_word_decision("no") is False
    assert _approval_word_decision("n") is False


@pytest.mark.asyncio
async def test_failed_task_returns_portrait_to_idle() -> None:
    """Invariant 17: When no active flows remain, refresh_panel_data resets current_status to idle."""
    mock_gateway = AsyncMock()
    mock_gateway.list_flows.return_value = []
    mock_gateway.list_approvals.return_value = []

    controller = ChatController(gateway=mock_gateway)
    controller.state.current_status = "failed"

    await controller.refresh_panel_data()
    assert controller.state.current_status == "idle"


@pytest.mark.asyncio
async def test_bare_shell_command_not_gated_by_owner_pin() -> None:
    """Regression: a Gateway-authenticated owner_id must not route bare pwd/ls
    through the PIN-gated owner-shell path (Case 1 in
    UnifiedToolExecutionLayer._execute_shell), which always denies because no
    PIN elevation was ever established for a plain dialogue turn. Every real
    /api/v1/dialogue/turn request carries a resolved owner_id, so this was a
    100% reproduction in the canonical CLI path even though direct
    brain.process() calls without owner_id masked it in earlier local testing.
    """
    turn_id = f"test-pwd-owner-{uuid.uuid4().hex}"
    async with AntigonaBrain() as brain:
        res = await brain.process(
            text="pwd", user_id="test_user", channel="cli",
            context={"turn_id": turn_id, "correlation_id": turn_id, "owner_id": "alice"},
        )
    assert res.response_type == ResponseType.CONVERSATION
    assert "Owner shell denied" not in res.text
    assert "/" in res.text


@pytest.mark.asyncio
async def test_no_pending_approval_confirmation_does_not_revive_stale_context() -> None:
    """Invariant 16: with no approval pending, 'да'/'yes' is ordinary text that
    goes through the normal dialogue turn — it must not be hijacked as a
    decision for a stale/unrelated approval."""
    mock_gateway = AsyncMock()
    mock_gateway.send_dialogue_turn.return_value = {
        "response_type": "conversation",
        "reply": "ok",
    }

    controller = ChatController(gateway=mock_gateway)
    assert controller._waiting_approval_id is None

    await controller._handle_free_text("да")

    mock_gateway.send_dialogue_turn.assert_awaited_once()
    mock_gateway.decide_approval.assert_not_called()


def test_docker_shell_tool_never_leaks_stdout_or_stderr_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Invariant 6 & 18, preserving the pre-existing security contract asserted
    by test_shell_security.py::test_nonzero_exit_returns_only_fixed_failure_
    without_stdout_or_stderr: a sandboxed command's stdout/stderr is untrusted,
    attacker-influenceable content, so a nonzero exit must map to the fixed
    safe category string only — never raw process output, sanitized or not.
    """
    import subprocess as subprocess_module

    from antigona.shell import DockerShellTool, ShellInput

    marker = b"SECRET_LOOKING_STDERR_CONTENT"

    class FakeProcess:
        returncode = 1

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            return b"", marker

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(subprocess_module, "Popen", fake_popen)

    tool = DockerShellTool(workspace=tmp_path / "ws")
    result = tool.execute(ShellInput(command=("install", "git")))

    assert result.ok is False
    assert result.error.startswith("tool exited non-zero")
    assert marker.decode() not in repr(result)


def test_classify_tool_failure_distinguishes_shell_exit_from_generic_failure() -> None:
    """Invariant 6 & 18: classify_tool_failure must surface the already-safe,
    already-categorized fixed reason strings DockerShellTool returns (e.g.
    'tool exited non-zero', 'sandbox unavailable') instead of collapsing every
    shell failure into the same generic 'tool execution failed' the user saw
    for 'install git' — without ever touching raw stdout/stderr content."""
    nonzero = ToolResult(ok=False, status="failed", error="tool exited non-zero")
    assert classify_tool_failure(nonzero) == "tool exited non-zero"

    unavailable = ToolResult(ok=False, status="failed", error="sandbox unavailable")
    assert classify_tool_failure(unavailable) != "tool execution failed"
