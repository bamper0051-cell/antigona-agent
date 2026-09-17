"""Unit tests for CLI multiline input (Defect D1 / Loop 5).

Verifies that:
A. handle_input with raw_input = "ANTIGONA FILE TEST\\n12345" -> ONE message / ONE send call
   with exact content "ANTIGONA FILE TEST\\n12345".
B. Single-line raw_input -> one message (UX regression check).
C. Empty / whitespace-only raw_input -> message is NOT sent.
D. create_prompt_key_bindings(): Esc+Enter inserts \\n into buffer (headless pipe test),
   Enter validates/submits.
E. Regression: Two consecutive handle_input calls with different lines -> TWO distinct messages
   (not concatenated/glued).
"""

from __future__ import annotations

from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.commands import CommandDisposition
from antigona.cli_ui.models import ChatMessageRole
from antigona.cli_ui.prompts import (
    create_prompt_key_bindings,
    create_prompt_session,
    read_prompt,
)
from antigona.core.control_plane import FlowStatus


class _FlowView:
    def __init__(self, status: FlowStatus, result: Any = None) -> None:
        self.status = status
        self.result = result
        self.result_data = result
        self.error = None
        self.error_message = None


class _FakeGateway:
    """Scripted gateway tracking send_dialogue_turn calls."""

    def __init__(self, script: list[FlowStatus] | None = None, flow_id: str = "flow-d5-test") -> None:
        self._script = list(script or [FlowStatus.QUEUED, FlowStatus.DONE])
        self._idx = 0
        self.flow_id = flow_id
        self.turn_calls: list[str] = []
        self.submit_count = 0

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> dict[str, Any]:
        self.turn_calls.append(text)
        return {
            "reply": "Задача принята и выполняется.",
            "session_id": session_id,
            "verified": None,
            "response_type": "task_accepted",
            "flow_id": self.flow_id,
            "requires_approval": False,
        }

    async def get_flow(self, flow_id: str) -> _FlowView:
        status = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return _FlowView(status, result="file written")

    async def wait_for_terminal(
        self,
        flow_id: str,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        cancel_event: Any = None,
    ) -> _FlowView:
        return _FlowView(FlowStatus.DONE, result="file written")

    async def list_flows(
        self,
        conversation_id: str | Any | None = "",
        status: Any | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        return []

    async def cancel(self, flow_id: str, reason: str = "") -> Any:
        return None

    async def list_approvals(
        self,
        status: str = "PENDING",
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        return []

    async def get_approval(self, approval_id: str) -> Any:
        return None

    async def decide_approval(self, approval_id: str, approve: bool) -> Any:
        return None

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self.submit_count += 1
        return {"id": self.flow_id, "status": "QUEUED"}

    async def steer(self, flow_id: str, command: Any) -> Any:
        return None

    async def health(self) -> Any:
        return {"status": "ok"}

    async def get_events(self, after_seq: int = 0, limit: int = 200) -> Any:
        return []

    async def session_info(self, session_id: str) -> Any:
        return None

    async def session_history(self, session_id: str, limit: int = 100) -> Any:
        return []

    async def list_commands(self, channel: str = "all") -> Any:
        return []

    async def memory_list(self, *, kind: str | None = None, query: str | None = None, limit: int = 50) -> Any:
        return []

    async def close(self) -> Any:
        return None


def _make_controller(gw: _FakeGateway) -> ChatController:
    return ChatController(gateway=gw, renderer=None, poll_interval_sec=0.001)


# ── Test A: Multiline two-line input ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_input_multiline_sends_single_message_with_exact_content() -> None:
    """A. handle_input with 'ANTIGONA FILE TEST\\n12345' -> ONE call with exact content."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    raw_input = "ANTIGONA FILE TEST\n12345"
    disp = await ctrl.handle_input(raw_input)

    assert disp == CommandDisposition.GATEWAY_EXECUTION
    assert len(gw.turn_calls) == 1
    assert gw.turn_calls[0] == "ANTIGONA FILE TEST\n12345"

    user_msgs = [
        m for m in ctrl.state.messages
        if (m.role.value if isinstance(m.role, ChatMessageRole) else str(m.role)) == "user"
    ]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "ANTIGONA FILE TEST\n12345"


@pytest.mark.asyncio
async def test_two_line_input_via_prompt_session_and_controller() -> None:
    """A (session integration): Esc+Enter then Enter -> ONE turn with exact content."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    with create_pipe_input() as pipe_inp:
        session = create_prompt_session(
            input=pipe_inp,
            output=DummyOutput(),
            multiline=True,
        )
        # Send line 1, Escape+Enter (\\x1b\\r), line 2, Enter (\\r)
        pipe_inp.send_text("ANTIGONA FILE TEST\x1b\r12345\r")
        raw = await read_prompt(session=session)

    assert raw == "ANTIGONA FILE TEST\n12345"

    disp = await ctrl.handle_input(raw)
    assert disp == CommandDisposition.GATEWAY_EXECUTION
    assert len(gw.turn_calls) == 1
    assert gw.turn_calls[0] == "ANTIGONA FILE TEST\n12345"


# ── Test B: Single-line input UX regression ──────────────────────────────────


@pytest.mark.asyncio
async def test_single_line_input_sends_single_message() -> None:
    """B. Single-line raw_input -> exactly one message sent (UX regression check)."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    raw_input = "создай файл test.txt"
    disp = await ctrl.handle_input(raw_input)

    assert disp == CommandDisposition.GATEWAY_EXECUTION
    assert len(gw.turn_calls) == 1
    assert gw.turn_calls[0] == "создай файл test.txt"

    user_msgs = [
        m for m in ctrl.state.messages
        if (m.role.value if isinstance(m.role, ChatMessageRole) else str(m.role)) == "user"
    ]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "создай файл test.txt"


@pytest.mark.asyncio
async def test_single_line_prompt_session_enter_submits() -> None:
    """B (session): Typing text and pressing Enter submits immediately."""
    with create_pipe_input() as pipe_inp:
        session = create_prompt_session(
            input=pipe_inp,
            output=DummyOutput(),
            multiline=True,
        )
        pipe_inp.send_text("создай файл test.txt\r")
        raw = await read_prompt(session=session)

    assert raw == "создай файл test.txt"


# ── Test C: Empty / whitespace-only input ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blank_input",
    [
        "",
        "   ",
        "\t",
        "\n",
        "\n\n\n",
        "  \n  \n  ",
        "\t\r\n",
    ],
)
async def test_empty_and_whitespace_input_not_sent(blank_input: str) -> None:
    """C. Empty or whitespace-only raw_input -> message is NOT sent (NOOP / LOCAL_ACTION)."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    disp = await ctrl.handle_input(blank_input)

    assert disp == CommandDisposition.LOCAL_ACTION
    assert len(gw.turn_calls) == 0
    assert len(ctrl.state.messages) == 0


# ── Test D: Key bindings (Esc+Enter -> \n in buffer, Enter -> submit) ─────────


@pytest.mark.asyncio
async def test_prompt_key_bindings_esc_enter_inserts_newline_and_enter_submits() -> None:
    """D. create_prompt_key_bindings(): Esc+Enter inserts \\n, Enter validates/submits."""
    with create_pipe_input() as pipe_inp:
        session = create_prompt_session(
            input=pipe_inp,
            output=DummyOutput(),
            multiline=True,
        )
        # Type "line1", then Escape+Enter (\x1b\r), then "line2", then Enter (\r)
        pipe_inp.send_text("line1\x1b\rline2\r")
        raw = await read_prompt(session=session)

    assert raw == "line1\nline2"


def test_create_prompt_key_bindings_structure() -> None:
    """D (structure): create_prompt_key_bindings registers escape+enter and enter."""
    kb = create_prompt_key_bindings()
    assert kb is not None
    bindings = kb.bindings
    keys = [b.keys for b in bindings]
    assert any(("escape", "enter") == k or ("escape", "c-m") == k for k in keys)
    assert any(("enter",) == k or ("c-m",) == k for k in keys)


# ── Test E: Regression - two consecutive handle_input calls ───────────────────


@pytest.mark.asyncio
async def test_two_consecutive_handle_input_calls_send_two_separate_messages() -> None:
    """E. Two consecutive handle_input calls -> TWO distinct messages (no concatenation)."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    first_input = "ANTIGONA FILE TEST\n12345"
    second_input = "вторая команда"

    disp1 = await ctrl.handle_input(first_input)
    assert disp1 == CommandDisposition.GATEWAY_EXECUTION

    disp2 = await ctrl.handle_input(second_input)
    assert disp2 == CommandDisposition.GATEWAY_EXECUTION

    assert len(gw.turn_calls) == 2
    assert gw.turn_calls[0] == first_input
    assert gw.turn_calls[1] == second_input

    user_msgs = [
        m for m in ctrl.state.messages
        if (m.role.value if isinstance(m.role, ChatMessageRole) else str(m.role)) == "user"
    ]
    assert len(user_msgs) == 2
    assert user_msgs[0].content == first_input
    assert user_msgs[1].content == second_input


# ── Bonus regression: Paste with newlines ─────────────────────────────────────


@pytest.mark.asyncio
async def test_bracketed_paste_multiline_preserved() -> None:
    """Bracketed paste containing newlines -> preserved as single turn."""
    gw = _FakeGateway()
    ctrl = _make_controller(gw)

    with create_pipe_input() as pipe_inp:
        session = create_prompt_session(
            input=pipe_inp,
            output=DummyOutput(),
            multiline=True,
        )
        pipe_inp.send_text("\x1b[200~ANTIGONA FILE TEST\n12345\x1b[201~\r")
        raw = await read_prompt(session=session)

    assert raw == "ANTIGONA FILE TEST\n12345"
    await ctrl.handle_input(raw)

    assert gw.turn_calls == ["ANTIGONA FILE TEST\n12345"]
