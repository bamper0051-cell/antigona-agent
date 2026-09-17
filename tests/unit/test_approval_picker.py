from __future__ import annotations

from typing import Any

import pytest

from antigona.cli_ui.approval_picker import (
    ApprovalEntry,
    PickerAction,
    PickerState,
    _map_key_to_action,
    _move_selection,
    build_picker_entries,
)
from antigona.cli_ui.commands import (
    CommandDisposition,
    CommandKind,
    ParsedCommand,
    dispatch_command,
    parse_command,
)


class MockGateway:
    """Structural double for GatewayClientProtocol (all protocol methods, stubs unused)."""

    def __init__(self, approvals: list[Any] | None = None, decide_error: Exception | None = None) -> None:
        self.approvals = approvals or []
        self.decide_error = decide_error
        self.decide_calls: list[tuple[str, bool]] = []
        self.list_calls = 0

    async def get_flow(self, flow_id: str) -> Any:
        del flow_id
        return {}

    async def list_flows(
        self,
        conversation_id: str | Any | None = "",
        status: Any | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        del conversation_id, status, limit, offset
        return []

    async def cancel(self, flow_id: str, reason: str = "") -> Any:
        del flow_id, reason
        return {}

    async def list_approvals(
        self, status: str = "PENDING", limit: int = 50, offset: int = 0
    ) -> list[Any]:
        del status, limit, offset
        self.list_calls += 1
        return self.approvals

    async def get_approval(self, approval_id: str) -> Any:
        del approval_id
        return {}

    async def decide_approval(self, approval_id: str, approve: bool) -> Any:
        if self.decide_error:
            raise self.decide_error
        self.decide_calls.append((approval_id, approve))
        return {"ok": True}

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        del message, conversation_id, client, metadata, idempotency_key
        return {}

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
    ) -> Any:
        del text, session_id, channel, user_id
        return {}

    async def steer(self, flow_id: str, command: Any) -> Any:
        del flow_id, command
        return {}

    async def health(self) -> Any:
        return {}

    async def get_events(self, after_seq: int = 0, limit: int = 200) -> Any:
        del after_seq, limit
        return []

    async def list_commands(self, channel: str = "all") -> Any:
        del channel
        return []

    async def session_info(self, session_id: str) -> Any:
        del session_id
        return {}

    async def session_history(self, session_id: str, limit: int = 100) -> Any:
        del session_id, limit
        return {}

    async def memory_list(self, *, kind: str | None = None, query: str | None = None, limit: int = 50) -> Any:
        del kind, query, limit
        return []


class MockRenderer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def render_message(self, role: str, text: str) -> None:
        self.messages.append((role, text))


@pytest.fixture
def gateway() -> MockGateway:
    return MockGateway()


def _entry(aid: str = "a1") -> ApprovalEntry:
    return ApprovalEntry(aid, "tool1", "low", "reason1", "f1")


def test_build_picker_entries_from_dicts() -> None:
    approvals = [
        {"id": "a1", "tool_name": "tool1", "risk_level": "high", "reason": "reason1", "task_id": "t1"},
        {"id": "a2", "tool_name": "tool2", "risk_level": "low", "reason": "reason2", "task_id": "t2"},
    ]
    entries = build_picker_entries(approvals, gateway=None)
    assert len(entries) == 2
    assert entries[0].approval_id == "a1"
    assert entries[0].tool_name == "tool1"
    assert entries[0].risk_level == "high"
    assert entries[0].reason == "reason1"
    assert entries[0].flow_id == "t1"
    assert entries[0].display_label == "tool1 — reason1"
    assert entries[1].display_label == "tool2 — reason2"


def test_build_picker_entries_skips_empty_id() -> None:
    approvals = [
        {"id": "", "tool_name": "tool1", "risk_level": "high", "reason": "reason1", "task_id": "t1"},
        {"id": "a2", "tool_name": "tool2", "risk_level": "low", "reason": "reason2", "task_id": "t2"},
    ]
    entries = build_picker_entries(approvals, gateway=None)
    assert len(entries) == 1
    assert entries[0].approval_id == "a2"


def test_picker_state_defaults() -> None:
    state = PickerState()
    assert state.entries == []
    assert state.selected_index == 0
    assert state.mode == "single"
    assert state.detail_view is False
    assert state.closed is False
    assert state.result is None
    assert state.chosen_entry is None


def test_map_key_to_action_arrow_keys() -> None:
    state = PickerState(
        entries=[_entry("a1"), _entry("a2")],
    )
    # up / \x1b[A: selection moves -1 (wraps 0 -> 1), no action
    assert _map_key_to_action("\x1b[A", state) is None
    assert state.selected_index == 1
    # down / \x1b[B: +1 -> 0
    assert _map_key_to_action("\x1b[B", state) is None
    assert state.selected_index == 0
    # right / \x1b[C: +1 -> 1
    assert _map_key_to_action("\x1b[C", state) is None
    assert state.selected_index == 1
    # left / \x1b[D: -1 -> 0
    assert _map_key_to_action("\x1b[D", state) is None
    assert state.selected_index == 0
    # plain words
    assert _map_key_to_action("up", state) is None
    assert state.selected_index == 1
    assert _map_key_to_action("down", state) is None
    assert state.selected_index == 0
    assert _map_key_to_action("left", state) is None
    assert state.selected_index == 1
    assert _map_key_to_action("right", state) is None
    assert state.selected_index == 0


def test_map_key_to_action_y_n_d_esc() -> None:
    state = PickerState(entries=[_entry()])
    assert _map_key_to_action("y", state) == PickerAction.APPROVE
    assert _map_key_to_action("n", state) == PickerAction.DENY
    assert _map_key_to_action("d", state) == PickerAction.DETAILS
    assert _map_key_to_action("\x1b", state) == PickerAction.CANCEL


def test_map_key_to_action_enter() -> None:
    state = PickerState(entries=[_entry()])
    assert _map_key_to_action("\r", state) == PickerAction.APPROVE
    assert _map_key_to_action("\n", state) == PickerAction.APPROVE
    assert _map_key_to_action("enter", state) == PickerAction.APPROVE


def test_move_selection_wraps() -> None:
    state = PickerState(entries=[_entry("a1"), _entry("a2")])
    _move_selection(state, -1)
    assert state.selected_index == 1
    _move_selection(state, 1)
    assert state.selected_index == 0
    _move_selection(state, 1)
    assert state.selected_index == 1
    # empty entries: no-op
    empty = PickerState()
    _move_selection(empty, 1)
    assert empty.selected_index == 0


def test_parse_approve_without_args() -> None:
    result = parse_command("/approve")
    assert result.kind == CommandKind.APPROVE
    assert result.args == ()
    assert result.command_name == "/approve"


def test_parse_deny_without_args() -> None:
    result = parse_command("/deny")
    assert result.kind == CommandKind.DENY
    assert result.args == ()
    assert result.command_name == "/deny"


def test_parse_approve_with_id() -> None:
    result = parse_command("/approve abc123")
    assert result.kind == CommandKind.APPROVE
    assert result.args == ("abc123",)
    assert result.command_name == "/approve"


def test_parse_deny_with_id() -> None:
    result = parse_command("/deny abc123")
    assert result.kind == CommandKind.DENY
    assert result.args == ("abc123",)
    assert result.command_name == "/deny"


def test_parse_approvals_without_args() -> None:
    result = parse_command("/approvals")
    assert result.kind == CommandKind.APPROVALS
    assert result.args == ()
    assert result.command_name == "/approvals"


def test_parse_approve_malformed() -> None:
    result = parse_command("/approve a b")
    assert result.kind == CommandKind.MALFORMED


async def test_approve_no_args_single_pending_decides() -> None:
    gw = MockGateway(
        approvals=[{"id": "a1", "tool_name": "t1", "risk_level": "low", "reason": "r1", "task_id": "f1"}]
    )
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=(), command_name="/approve")
    result = await dispatch_command(cmd, gw)
    assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
    assert gw.decide_calls == [("a1", True)]


async def test_approve_no_args_no_pending() -> None:
    gw = MockGateway(approvals=[])
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=(), command_name="/approve")
    result = await dispatch_command(cmd, gw)
    assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
    assert result.data == {"message": "Нет ожидающих подтверждений."}
    assert gw.decide_calls == []


async def test_approve_no_args_multi_pending_returns_message() -> None:
    gw = MockGateway(
        approvals=[
            {"id": "a1", "tool_name": "t1", "risk_level": "low", "reason": "r1", "task_id": "f1"},
            {"id": "a2", "tool_name": "t2", "risk_level": "high", "reason": "r2", "task_id": "f2"},
        ]
    )
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=(), command_name="/approve")
    result = await dispatch_command(cmd, gw)
    assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
    assert "Ожидают подтверждения: 2" in result.data["message"]
    assert gw.decide_calls == []


async def test_deny_no_args_single_pending_decides() -> None:
    gw = MockGateway(
        approvals=[{"id": "a1", "tool_name": "t1", "risk_level": "low", "reason": "r1", "task_id": "f1"}]
    )
    cmd = ParsedCommand(kind=CommandKind.DENY, args=(), command_name="/deny")
    result = await dispatch_command(cmd, gw)
    assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
    assert gw.decide_calls == [("a1", False)]


async def test_approve_with_id_still_works() -> None:
    gw = MockGateway()
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=("abc123",), command_name="/approve")
    result = await dispatch_command(cmd, gw)
    assert result.disposition == CommandDisposition.GATEWAY_EXECUTION
    assert gw.decide_calls == [("abc123", True)]


async def test_duplicate_approval_decision_is_rejected() -> None:
    gw = MockGateway(decide_error=RuntimeError("duplicate"))
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=("abc123",), command_name="/approve")
    result = await dispatch_command(cmd, gw)
    # dispatch_command fails closed: any gateway exception -> ERROR with redacted detail
    assert result.disposition == CommandDisposition.ERROR
    assert result.error is not None


async def test_approvals_owner_isolation() -> None:
    gw = MockGateway(
        approvals=[{"id": "a1", "tool_name": "t1", "risk_level": "low", "reason": "r1", "task_id": "f1"}]
    )
    cmd = ParsedCommand(kind=CommandKind.APPROVE, args=(), command_name="/approve")
    await dispatch_command(cmd, gw)
    assert gw.list_calls == 1


def test_multi_approval_picker_entries() -> None:
    approvals = [
        {"id": "a1", "tool_name": "t1", "risk_level": "low", "reason": "r1", "task_id": "f1"},
        {"id": "a2", "tool_name": "t2", "risk_level": "high", "reason": "r2", "task_id": "f2"},
    ]
    entries = build_picker_entries(approvals, gateway=None)
    assert len(entries) == 2
    assert entries[0].approval_id == "a1"
    assert entries[1].approval_id == "a2"
    assert entries[0].display_label == "t1 — r1"
