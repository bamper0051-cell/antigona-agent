"""Tests for CLI command parsing — slash-command routing, fail-closed safety.

Ensures that:
- Unknown slash commands (/gey) return UNKNOWN and NEVER reach Gateway.
- Known commands without required args (/get, /approve, /deny, /cancel) return MALFORMED.
- Only non-slash free text returns UNSUPPORTED and routes to POST /tasks.
- Gateway error details are withheld at the CLI presentation boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.commands import (
    CommandDisposition,
    CommandKind,
    ParsedCommand,
    dispatch_command,
    is_valid_resource_id,
    parse_command,
)

# ── parse_command: classification ─────────────────────────────────────────────


class TestParseCommand:
    """parse_command classifies raw input correctly."""

    def test_unknown_slash_command_returns_unknown(self) -> None:
        """/gey → UNKNOWN, never UNSUPPORTED or MALFORMED that could hit Gateway."""
        result = parse_command("/gey")
        assert result.kind == CommandKind.UNKNOWN
        assert result.command_name == "/gey"

    def test_unknown_slash_with_args(self) -> None:
        """/foobar baz qux → UNKNOWN with args preserved."""
        result = parse_command("/foobar baz qux")
        assert result.kind == CommandKind.UNKNOWN
        assert result.args == ("baz", "qux")

    def test_typo_command(self) -> None:
        """/statis → UNKNOWN (not STATUS slash-command)."""
        result = parse_command("/statis")
        assert result.kind == CommandKind.UNKNOWN

    def test_get_without_args_returns_malformed(self) -> None:
        """/get (no arg) → MALFORMED, NOT UNSUPPORTED."""
        result = parse_command("/get")
        assert result.kind == CommandKind.MALFORMED
        assert result.command_name == "/get"

    def test_get_with_valid_id(self) -> None:
        """/get abcd-1234 → GET with args=('abcd-1234',)."""
        result = parse_command("/get abcd-1234")
        assert result.kind == CommandKind.GET
        assert result.args == ("abcd-1234",)

    def test_get_with_invalid_id(self) -> None:
        """/get ../../../etc → MALFORMED (invalid resource ID)."""
        result = parse_command("/get ../../../etc")
        assert result.kind == CommandKind.MALFORMED
        assert result.command_name == "/get"

    def test_status_without_args(self) -> None:
        """/status (no args) → MALFORMED (requires flow_id)."""
        result = parse_command("/status")
        assert result.kind == CommandKind.MALFORMED
        assert result.command_name == "/status"

    def test_status_with_valid_id(self) -> None:
        """/status flow-xyz → STATUS with args=('flow-xyz',)."""
        result = parse_command("/status flow-xyz")
        assert result.kind == CommandKind.STATUS
        assert result.args == ("flow-xyz",)

    def test_approve_without_args_is_valid(self) -> None:
        """/approve (no arg) → APPROVE (picker/auto-single flow, no manual ID)."""
        result = parse_command("/approve")
        assert result.kind == CommandKind.APPROVE
        assert result.args == ()
        assert result.command_name == "/approve"

    def test_deny_without_args_is_valid(self) -> None:
        """/deny (no arg) → DENY (picker/auto-single flow, no manual ID)."""
        result = parse_command("/deny")
        assert result.kind == CommandKind.DENY
        assert result.args == ()
        assert result.command_name == "/deny"

    def test_cancel_without_args_returns_malformed(self) -> None:
        """/cancel (no arg) → MALFORMED."""
        result = parse_command("/cancel")
        assert result.kind == CommandKind.MALFORMED
        assert result.command_name == "/cancel"

    def test_free_text_returns_unsupported(self) -> None:
        """ "write hello world" → UNSUPPORTED (routes to POST /tasks)."""
        result = parse_command("write hello world")
        assert result.kind == CommandKind.UNSUPPORTED

    def test_list_returns_list(self) -> None:
        """/list → LIST."""
        result = parse_command("/list")
        assert result.kind == CommandKind.LIST
        assert result.args == ()

    def test_approvals_returns_approvals(self) -> None:
        """/approvals → APPROVALS."""
        result = parse_command("/approvals")
        assert result.kind == CommandKind.APPROVALS
        assert result.args == ()

    def test_help_returns_help(self) -> None:
        """/help → HELP."""
        result = parse_command("/help")
        assert result.kind == CommandKind.HELP
        assert result.args == ()

    def test_exit_returns_exit(self) -> None:
        """/exit → EXIT."""
        result = parse_command("/exit")
        assert result.kind == CommandKind.EXIT
        assert result.args == ()

    def test_quit_returns_exit(self) -> None:
        """/quit → EXIT (alias)."""
        result = parse_command("/quit")
        assert result.kind == CommandKind.EXIT

    def test_padded_slash_command(self) -> None:
        """ " /get" preserves leading space — MALFORMED at parser level (chat.py catches padded case upstream)."""
        result = parse_command(" /get")
        assert result.kind == CommandKind.MALFORMED

    def test_steer_requires_flow_id(self) -> None:
        """/steer без flow_id → MALFORMED (fail-closed, не идёт в Gateway)."""
        result = parse_command("/steer")
        assert result.kind == CommandKind.MALFORMED
        assert result.command_name == "/steer"

    def test_steer_with_args(self) -> None:
        """/steer <flow_id> <текст> → STEER с аргументами."""
        result = parse_command("/steer flow-12345 добавь проверку")
        assert result.kind == CommandKind.STEER
        assert result.args == ("flow-12345", "добавь проверку")
        assert result.command_name == "/steer"

    def test_steer_malformed_flow_id(self) -> None:
        """/steer с невалидным flow_id → MALFORMED."""
        result = parse_command("/steer foo/bar baz")
        assert result.kind == CommandKind.MALFORMED


# ── Parameterised: all slash inputs → non-UNSUPPORTED ─────────────────────────


class TestSlashNeverUnsupported:
    """Every slash-prefixed input must parse to something other than UNSUPPORTED.

    UNSUPPORTED is the only classification that may route to POST /tasks.
    A structural guard in ChatController also catches any UNSUPPORTED slash
    input, but the parser itself must never classify a recognisable slash
    command as UNSUPPORTED.
    """

    @pytest.mark.parametrize(
        "raw,expected_kind",
        [
            ("/steer", CommandKind.MALFORMED),
            ("/gey", CommandKind.UNKNOWN),
            ("/unknown", CommandKind.UNKNOWN),
            ("/foobar arg", CommandKind.UNKNOWN),
            ("/get", CommandKind.MALFORMED),
            ("/get flow-123", CommandKind.GET),
            ("/approve", CommandKind.APPROVE),
            ("/deny", CommandKind.DENY),
            ("/cancel", CommandKind.MALFORMED),
            ("/status", CommandKind.MALFORMED),
            ("/status flow-123", CommandKind.STATUS),
            ("/list", CommandKind.LIST),
            ("/help", CommandKind.HELP),
            ("/exit", CommandKind.EXIT),
            ("/quit", CommandKind.EXIT),
            ("/approvals", CommandKind.APPROVALS),
        ],
    )
    def test_slash_input_never_unsupported(self, raw: str, expected_kind: CommandKind) -> None:
        """/<anything> should never be UNSUPPORTED."""
        result = parse_command(raw)
        assert result.kind == expected_kind, (
            f"Expected {expected_kind} for {raw!r}, got {result.kind}"
        )
        assert result.kind is not CommandKind.UNSUPPORTED, (
            f"Slash input {raw!r} must never be UNSUPPORTED"
        )


# ── dispatch_command: known-command routing ───────────────────────────────────


class _MockGateway:
    """Minimal mock GatewayClient for dispatch_command tests."""

    def __init__(self, *, flows: dict[str, Any] | None = None) -> None:
        self.flows = flows or {}
        self.last_flow_id: str | None = None

    async def get_flow(self, flow_id: str) -> Any:
        self.last_flow_id = flow_id
        if flow_id in self.flows:
            return self.flows[flow_id]
        raise ValueError(f"Flow not found: {flow_id}")

    async def list_flows(self) -> Any:
        return list(self.flows.values())

    async def cancel(self, flow_id: str, reason: str = "") -> Any:
        if flow_id not in self.flows:
            raise ValueError(f"Flow not found: {flow_id}")
        return {"status": "CANCELLED"}

    async def list_approvals(self) -> Any:
        return []

    async def get_approval(self, approval_id: str) -> Any:
        raise ValueError(f"Approval not found: {approval_id}")

    async def decide_approval(self, approval_id: str, approve: bool) -> Any:
        raise ValueError(f"Approval not found: {approval_id}")

    async def submit_task(self, **kwargs: Any) -> Any:
        return {"id": "mock-task"}


class TestDispatchCommand:
    """dispatch_command routes correctly and never lets unknown commands hit Gateway."""

    async def test_unknown_command_returns_local_action(self) -> None:
        """UNKNOWN → LOCAL_ACTION with 'Unknown command' message."""
        cmd = ParsedCommand(kind=CommandKind.UNKNOWN, args=(), command_name="/gey")
        result = await dispatch_command(cmd)
        assert result.disposition == CommandDisposition.LOCAL_ACTION
        assert "Unknown command" in str(result.data)

    async def test_unknown_command_never_calls_gateway(self) -> None:
        """UNKNOWN → no Gateway call made."""
        mock = _MockGateway()
        cmd = ParsedCommand(kind=CommandKind.UNKNOWN, args=(), command_name="/gey")
        await dispatch_command(cmd, mock)
        assert mock.last_flow_id is None  # no Gateway call

    async def test_malformed_returns_fail_closed(self) -> None:
        """MALFORMED → FAIL_CLOSED with descriptive error."""
        cmd = ParsedCommand(kind=CommandKind.MALFORMED, args=(), command_name="/get")
        result = await dispatch_command(cmd)
        assert result.disposition == CommandDisposition.FAIL_CLOSED
        assert "malformed" in str(result.error).lower()

    async def test_unsupported_returns_fail_closed(self) -> None:
        """UNSUPPORTED → FAIL_CLOSED with descriptive error."""
        cmd = ParsedCommand(kind=CommandKind.UNSUPPORTED, args=(), command_name="text")
        result = await dispatch_command(cmd)
        assert result.disposition == CommandDisposition.FAIL_CLOSED

    async def test_get_with_valid_id_returns_gateway_execution(self) -> None:
        """GET with valid ID → GATEWAY_EXECUTION."""
        mock = _MockGateway(flows={"flow-1": {"id": "flow-1", "status": "DONE"}})
        cmd = ParsedCommand(kind=CommandKind.GET, args=("flow-1",), command_name="/get")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert mock.last_flow_id == "flow-1"

    async def test_get_without_args_no_gateway_call_from_dispatch(self) -> None:
        """GET without args should not hit Gateway in dispatch_command."""
        # This test verifies dispatch_command itself rejects GET with no args.
        # Returning FAIL_CLOSED is the correct dispatch-level behaviour.
        mock = _MockGateway()
        cmd = ParsedCommand(kind=CommandKind.GET, args=(), command_name="/get")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.FAIL_CLOSED
        assert mock.last_flow_id is None

    async def test_get_nonexistent_flow_propagates_error(self) -> None:
        """GET for non-existent flow → ERROR without raw exception details."""
        mock = _MockGateway()
        cmd = ParsedCommand(kind=CommandKind.GET, args=("no-such-flow",), command_name="/get")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.ERROR
        assert result.error == "Gateway operation failed; details withheld: [REDACTED]"
        assert "no-such-flow" not in str(result.error)

    async def test_list_returns_gateway_execution(self) -> None:
        """LIST → GATEWAY_EXECUTION."""
        mock = _MockGateway()
        cmd = ParsedCommand(kind=CommandKind.LIST, args=(), command_name="/list")
        result = await dispatch_command(cmd, mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION

    async def test_approve_without_args_returns_fail_closed(self) -> None:
        """/approve without args from dispatch → FAIL_CLOSED."""
        cmd = ParsedCommand(kind=CommandKind.MALFORMED, args=(), command_name="/approve")
        result = await dispatch_command(cmd)
        assert result.disposition == CommandDisposition.FAIL_CLOSED

    async def test_approve_with_valid_id_calls_gateway(self) -> None:
        """/approve with valid ID → GATEWAY_EXECUTION (error from mock)."""
        mock = _MockGateway()
        cmd = ParsedCommand(kind=CommandKind.APPROVE, args=("ap-123",), command_name="/approve")
        result = await dispatch_command(cmd, mock)
        # The mock raises ValueError, so it becomes ERROR
        assert result.disposition == CommandDisposition.ERROR
        # But the key point is: dispatch_command TRIED the Gateway call
        # (it didn't short-circuit as malformed)


# ── Integration: full parse + dispatch pipeline ───────────────────────────────


class TestParseThenDispatch:
    """End-to-end: parse raw input, then dispatch the result."""

    async def _dispatch_raw(self, raw: str, mock: Any | None = None) -> Any:
        cmd = parse_command(raw)
        return await dispatch_command(cmd, mock)

    async def test_unknown_typo_command_never_hits_gateway(self) -> None:
        """/statis → parse UNKNOWN → dispatch LOCAL_ACTION, no Gateway call."""
        mock = _MockGateway()
        result = await self._dispatch_raw("/statis", mock)
        assert result.disposition == CommandDisposition.LOCAL_ACTION
        assert mock.last_flow_id is None

    async def test_get_no_arg(self) -> None:
        """/get → parse MALFORMED → dispatch FAIL_CLOSED."""
        mock = _MockGateway()
        result = await self._dispatch_raw("/get", mock)
        assert result.disposition == CommandDisposition.FAIL_CLOSED
        assert mock.last_flow_id is None

    async def test_approve_no_arg(self) -> None:
        """/approve → APPROVE, dispatch lists approvals; none pending → GATEWAY_EXECUTION message."""
        mock = _MockGateway()
        result = await self._dispatch_raw("/approve", mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert result.data == {"message": "Нет ожидающих подтверждений."}
        assert mock.last_flow_id is None

    async def test_deny_no_arg(self) -> None:
        """/deny → DENY, dispatch lists approvals; none pending → GATEWAY_EXECUTION message."""
        mock = _MockGateway()
        result = await self._dispatch_raw("/deny", mock)
        assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
        assert result.data == {"message": "Нет ожидающих подтверждений."}
        assert mock.last_flow_id is None

    async def test_cancel_no_arg(self) -> None:
        """/cancel → parse MALFORMED → dispatch FAIL_CLOSED."""
        mock = _MockGateway()
        result = await self._dispatch_raw("/cancel", mock)
        assert result.disposition == CommandDisposition.FAIL_CLOSED
        assert mock.last_flow_id is None

    async def test_free_text_routes_to_submit(self) -> None:
        """'write hello world' → parse UNSUPPORTED → dispatch ..."""
        # dispatch_command doesn't have submit_task for UNSUPPORTED.
        # This is covered by ChatController-level tests below.
        mock = _MockGateway()
        result = await self._dispatch_raw("write hello world", mock)
        # UNSUPPORTED is FAIL_CLOSED at dispatch_command level
        # (ChatController handles the submit_task call before dispatch)
        assert result.disposition == CommandDisposition.FAIL_CLOSED

    async def test_get_with_approval_id_returns_gateway_error(self) -> None:
        """/get ap-123 with approval ID format (valid resource_id) → Gateway ERROR."""
        # If the approval ID passes is_valid_resource_id, it goes to GET
        mock = _MockGateway()
        result = await self._dispatch_raw("/get ap-123", mock)
        assert result.disposition == CommandDisposition.ERROR
        assert result.error == "Gateway operation failed; details withheld: [REDACTED]"
        assert "ap-123" not in str(result.error)


# ── ChatController: structural guard for slash-prefixed input ─────────────────


class _TrackedGatewayMock:
    """GatewayClientProtocol mock that tracks submit_task and turn calls."""

    def __init__(self) -> None:
        self.submit_calls: list[str] = []
        self.turn_calls: list[str] = []
        self.last_flow_id: str | None = None

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> Any:
        self.turn_calls.append(text)
        # Task-like free text → server core accepts the task.
        return {
            "reply": "Задача принята и выполняется.",
            "session_id": session_id,
            "verified": None,
            "response_type": "task_accepted",
            "flow_id": "mock-flow-123",
            "requires_approval": True,
        }

    async def get_flow(self, flow_id: str) -> Any:
        self.last_flow_id = flow_id
        raise ValueError(f"Flow not found: {flow_id}")

    async def list_flows(self) -> Any:
        return []

    async def cancel(self, flow_id: str, reason: str = "") -> Any:
        raise ValueError(f"Flow not found: {flow_id}")

    async def list_approvals(self) -> Any:
        return []

    async def get_approval(self, approval_id: str) -> Any:
        raise ValueError(f"Approval not found: {approval_id}")

    async def decide_approval(self, approval_id: str, approve: bool) -> Any:
        raise ValueError(f"Approval not found: {approval_id}")

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self.submit_calls.append(message)
        return {"id": "mock-flow-123", "status": "QUEUED"}

    def close(self) -> None:
        pass


class _FailingGatewayMock(_TrackedGatewayMock):
    async def get_flow(self, flow_id: str) -> Any:
        self.last_flow_id = flow_id
        raise RuntimeError("LEAK_SENTINEL_VALUE")

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> Any:
        self.turn_calls.append(text)
        raise RuntimeError("LEAK_SENTINEL_VALUE")

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self.submit_calls.append(message)
        raise RuntimeError("LEAK_SENTINEL_VALUE")


class TestChatControllerSlashGuard:
    """ChatController structural guard: slash-prefixed input never calls submit_task."""

    @pytest.mark.parametrize(
        "raw",
        [
            "/steer",
            "/gey",
            "/unknown",
            "/foobar arg",
            "/get",
            "/approve",
            "/deny",
            "/cancel",
        ],
    )
    async def test_slash_input_never_calls_submit_task(self, raw: str) -> None:
        """/<anything> → submit_task MUST NOT be called."""
        mock = _TrackedGatewayMock()
        ctrl = ChatController(gateway=mock)
        result = await ctrl.handle_input(raw)
        # Must be LOCAL_ACTION, FAIL_CLOSED, or GATEWAY_EXECUTION — never
        # GATEWAY_EXECUTION produced by submit_task (approve/deny now dispatch
        # to the approvals path without a manual ID).
        assert result in (
            CommandDisposition.LOCAL_ACTION,
            CommandDisposition.FAIL_CLOSED,
            CommandDisposition.GATEWAY_EXECUTION,
        ), (
            f"Slash input {raw!r} produced {result}, expected local handling"
        )
        assert len(mock.submit_calls) == 0, (
            f"submit_task was called {len(mock.submit_calls)} time(s) for slash input {raw!r}: {mock.submit_calls}"
        )

    async def test_get_with_flow_id_allowed(self) -> None:
        """/get <flow_id> → GATEWAY_EXECUTION (allowed, not submit_task)."""
        mock = _TrackedGatewayMock()
        ctrl = ChatController(gateway=mock)
        _ = await ctrl.handle_input("/get valid-flow-123")
        # Should dispatch to Gateway get_flow, which raises ValueError
        # so we get ERROR, but the point is: it goes through dispatch, not submit_task
        assert len(mock.submit_calls) == 0
        assert mock.last_flow_id == "valid-flow-123"

    async def test_free_text_calls_submit_task_once(self) -> None:
        """'создай файл test.txt' → Turn API called exactly once; task created
        by the server core, never by a local submit_task."""
        mock = _TrackedGatewayMock()
        ctrl = ChatController(gateway=mock)
        result = await ctrl.handle_input("создай файл test.txt")
        assert result == CommandDisposition.GATEWAY_EXECUTION
        assert len(mock.turn_calls) == 1, (
            f"Expected 1 send_dialogue_turn call, got {len(mock.turn_calls)}"
        )
        assert mock.turn_calls[0] == "создай файл test.txt", (
            f"turn message mismatch: {mock.turn_calls[0]!r}"
        )
        # Stage 1: no local submit — the server core owns task creation.
        assert len(mock.submit_calls) == 0

    async def test_free_text_preserves_input_exactly(self) -> None:
        """Free text preserves exact input including special chars."""
        mock = _TrackedGatewayMock()
        ctrl = ChatController(gateway=mock)
        free_text = "напиши файл с текстом 'hello world' в /tmp/test.txt"
        result = await ctrl.handle_input(free_text)
        assert result == CommandDisposition.GATEWAY_EXECUTION
        assert len(mock.turn_calls) == 1
        assert mock.turn_calls[0] == free_text

    async def test_free_text_failure_withholds_raw_exception(self) -> None:
        mock = _FailingGatewayMock()
        ctrl = ChatController(gateway=mock)

        result = await ctrl.handle_input("создай файл test.txt")

        assert result == CommandDisposition.FAIL_CLOSED
        assert ctrl.state.terminal_outcome is not None
        message = str(ctrl.state.terminal_outcome.error_message)
        assert "LEAK_SENTINEL_VALUE" not in message

    async def test_dispatch_failure_withholds_raw_exception(self) -> None:
        mock = _FailingGatewayMock()
        command = ParsedCommand(kind=CommandKind.GET, args=("flow-123",), command_name="/get")

        result = await dispatch_command(command, mock)

        assert result.disposition == CommandDisposition.ERROR
        assert "LEAK_SENTINEL_VALUE" not in str(result.error)
        assert "[REDACTED]" in str(result.error)


# ── is_valid_resource_id ──────────────────────────────────────────────────────


class TestIsValidResourceId:
    """is_valid_resource_id rejects traversal, controls, and malformed IDs."""

    def test_empty_rejected(self) -> None:
        assert is_valid_resource_id("") is False

    def test_path_traversal_rejected(self) -> None:
        assert is_valid_resource_id("../etc") is False

    def test_valid_normal_id(self) -> None:
        assert is_valid_resource_id("flow-123_abc") is True

    def test_short_id(self) -> None:
        assert is_valid_resource_id("a") is True

    def test_max_length(self) -> None:
        assert is_valid_resource_id("a" * 36) is True

    def test_over_length_rejected(self) -> None:
        assert is_valid_resource_id("a" * 37) is False

    def test_non_ascii_rejected(self) -> None:
        assert is_valid_resource_id("флоу") is False

    def test_special_chars_rejected(self) -> None:
        assert is_valid_resource_id("flow?id=1") is False


# ── Informational command intents (/model, /providers, /bot) ─────────────────


class TestInformationalCommandIntents:
    """/model, /providers, /bot classify as INFORMATIONAL and route to the
    conversation turn (never /tasks), aligning CLI with the backend router."""

    @pytest.mark.parametrize(
        "cmd,kind", [("/model", CommandKind.MODEL), ("/providers", CommandKind.PROVIDER), ("/bot", CommandKind.INFORMATIONAL)]
    )
    def test_informational_command(self, cmd: str, kind: CommandKind) -> None:
        result = parse_command(cmd)
        assert result.kind == kind
        assert result.command_name == cmd

    @pytest.mark.parametrize("cmd", ["/model foo", "/providers extra"])
    def test_model_provider_accept_args(self, cmd: str) -> None:
        # /model and /providers accept an argument (set model / switch provider).
        assert parse_command(cmd).kind in (CommandKind.MODEL, CommandKind.PROVIDER)

    @pytest.mark.parametrize("cmd", ["/bot bar", "/keys x"])
    def test_bot_with_args_is_malformed(self, cmd: str) -> None:
        assert parse_command(cmd).kind == CommandKind.MALFORMED

    def test_unknown_verb_still_unknown(self) -> None:
        assert parse_command("/definitely-not-a-command").kind == CommandKind.UNKNOWN
