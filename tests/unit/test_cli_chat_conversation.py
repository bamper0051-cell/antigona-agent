"""Regression tests: `antigona chat` free text is a conversational *turn*.

Stage 1 (unified server core): the CLI is a THIN client. Free text goes through
``gateway.send_dialogue_turn()`` — the single /api/v1/dialogue/turn endpoint
owned by the server brain. There is NO local IntentRouter/DialogueEngine: the
server classifies and returns ``response_type``; the controller renders
accordingly:
- conversation / clarification / control / error → show reply, no TaskFlow;
- task_accepted → live wait on the returned flow (approval-aware);
- while an approval is pending, "да" / "нет" decide it without IDs.

The Gateway remains the only authority: the controller never declares success
by itself (the canonical wait_for_terminal gate is unchanged).
"""

from __future__ import annotations

from typing import Any

import pytest

import antigona.cli_ui.chat as chat_module
from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.models import ChatMessageRole, TerminalOutcomeStatus
from antigona.core.control_plane import FlowStatus


class _Approval:
    def __init__(self, aid: str, task_id: str) -> None:
        self.id = aid
        self.task_id = task_id
        self.tool_name = "workspace.write_text"
        self.risk_level = "medium"
        self.reason = "approval required"
        self.created_at: Any = None


class _FlowView:
    def __init__(self, status: FlowStatus, result: str | None = None) -> None:
        self.status = status
        self.result = result


class _FakeGateway:
    """Scripted gateway: send_dialogue_turn classifies like the server brain;
    get_flow replays a status script; decide_approval forces the flow to DONE
    (approve) or FAILED (deny)."""

    def __init__(
        self,
        script: list[FlowStatus],
        flow_id: str = "flow-123",
        result_text: str | None = None,
    ) -> None:
        self._script = list(script)
        self._idx = 0
        self.flow_id = flow_id
        self.submit_count = 0
        self.turn_calls: list[str] = []
        self.decide_calls: list[tuple[str, bool]] = []
        self.approvals = [_Approval("appr-1", flow_id)]
        self.result_text = result_text

    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> dict[str, Any]:
        """Emulate the server brain's classification (subset used by tests)."""
        self.turn_calls.append(text)
        low = text.strip().lower()
        # Task phrases → task_accepted with flow_id
        if "создай файл" in low:
            return {
                "reply": "Задача принята и выполняется.",
                "session_id": session_id,
                "verified": True,
                "response_type": "task_accepted",
                "flow_id": self.flow_id,
                "requires_approval": True,
            }
        # Clarification cases (ambiguous)
        if low in ("стоп",) or "не меняй конфигурацию" in low or "write hello world" in low:
            return {
                "reply": "Уточните, пожалуйста, что нужно сделать.",
                "session_id": session_id,
                "verified": True,
                "response_type": "clarification",
            }
        # Everything else → conversation
        return {
            "reply": "Привет! Чем могу помочь?",
            "session_id": session_id,
            "verified": True,
            "response_type": "conversation",
        }

    async def submit_task(
        self,
        message: str,
        conversation_id: str = "",
        client: str = "cli",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.submit_count += 1
        return {"id": self.flow_id, "status": "QUEUED"}

    async def get_flow(self, flow_id: str) -> _FlowView:
        status = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return _FlowView(status, result=self.result_text)

    async def list_flows(
        self,
        conversation_id: str | Any | None = "",
        status: Any | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]:
        return []

    async def cancel(self, flow_id: str, reason: str = "") -> Any:
        return None

    async def list_approvals(
        self, status: str = "PENDING", limit: int = 50, offset: int = 0
    ) -> list[_Approval]:
        return self.approvals

    async def get_approval(self, approval_id: str) -> Any:
        return self.approvals[0]

    async def decide_approval(self, approval_id: str, approve: bool) -> Any:
        self.decide_calls.append((approval_id, approve))
        if approve:
            self._script = [FlowStatus.DONE]
            self.result_text = "done-result"
        else:
            self._script = [FlowStatus.FAILED]
            self.result_text = None
        return None

    async def wait_for_terminal(
        self,
        flow_id: str,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        cancel_event: Any = None,
    ) -> _FlowView:
        return _FlowView(
            self._script[0] if self._script else FlowStatus.DONE,
            result=self.result_text,
        )


def _controller(gw: Any, **kw: Any) -> ChatController:
    return ChatController(
        gw,
        renderer=None,
        poll_interval_sec=0.001,
        max_poll_sec=5.0,
        conversation_id="cli-session",
        enable_animations=False,
        **kw,
    )


@pytest.mark.asyncio
async def test_chitchat_does_not_create_flow() -> None:
    """Greeting must be answered through the Turn API without a TaskFlow."""
    gw = _FakeGateway([FlowStatus.QUEUED])
    ctrl = _controller(gw)

    await ctrl.handle_input("Привет!")

    assert gw.turn_calls == ["Привет!"]
    assert gw.submit_count == 0
    assert ctrl.state.terminal_outcome is None
    assert ctrl.state.messages[-1].role is ChatMessageRole.ASSISTANT
    assert ctrl.state.messages[-1].content


@pytest.mark.asyncio
async def test_question_does_not_create_flow() -> None:
    """A question is conversation, not a task — no TaskFlow, no echo."""
    gw = _FakeGateway([FlowStatus.QUEUED])
    ctrl = _controller(gw)

    await ctrl.handle_input("Сколько будет 17 плюс 28? Ответь одним числом.")

    assert gw.submit_count == 0
    assert ctrl.state.terminal_outcome is None
    reply = ctrl.state.messages[-1].content
    assert ctrl.state.messages[-1].role is ChatMessageRole.ASSISTANT
    # The server brain reply must never echo the question.
    assert "17 плюс 28" not in reply


@pytest.mark.asyncio
async def test_task_phrase_creates_flow_and_waits() -> None:
    """task_accepted → the server created the flow; live wait blocks at approval."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    assert gw.turn_calls == ["создай файл test.txt"]
    assert gw.submit_count == 0  # task created by the server core, not locally
    assert ctrl.active_flow_id == "flow-123"
    assert ctrl._waiting_approval_id == "appr-1"
    assert ctrl.state.terminal_outcome is None


@pytest.mark.asyncio
async def test_natural_approval_yes_resumes_and_completes() -> None:
    """'да' while an approval is pending decides it without IDs and resumes
    the wait to a verified terminal outcome."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")
    await ctrl.handle_input("да")

    assert gw.decide_calls == [("appr-1", True)]
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.SUCCESS
    assert ctrl._waiting_approval_id is None


@pytest.mark.asyncio
async def test_natural_approval_no_denies() -> None:
    """'нет' while an approval is pending denies it (approve=False)."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.FAILED])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")
    await ctrl.handle_input("нет")

    assert gw.decide_calls == [("appr-1", False)]
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.FAILED


@pytest.mark.asyncio
async def test_steer_phrase_never_creates_new_task() -> None:
    """A steering-style phrase must not create a new TaskFlow (no false
    task on conversation), even when an active flow exists."""
    gw = _FakeGateway([FlowStatus.DONE])
    ctrl = _controller(gw)
    ctrl.active_flow_id = "flow-123"

    await ctrl.handle_input("не меняй конфигурацию, используй systemd unit")

    assert gw.submit_count == 0
    assert ctrl.state.terminal_outcome is None


@pytest.mark.asyncio
async def test_approval_words_not_hijacked_without_pending_approval() -> None:
    """Without a pending approval, 'давай создай файл x.txt' must still route
    as a task — approval vocabulary is only active while waiting."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("давай создай файл x.txt")

    assert gw.submit_count == 0  # created via Turn API, not local submit
    assert ctrl.active_flow_id == "flow-123"


class _ListViewApprovalGateway(_FakeGateway):
    """Gateway whose list_approvals returns a pydantic-like wrapper exposing
    ``.items`` as a list attribute (not callable) — the live ApprovalListView
    shape that broke _first_approval_for_flow against the real gateway."""

    async def list_approvals(
        self, status: str = "PENDING", limit: int = 50, offset: int = 0
    ) -> Any:
        return _ApprovalListView(self.approvals)


class _ApprovalListView:
    def __init__(self, items: list[_Approval]) -> None:
        self.items = items


@pytest.mark.asyncio
async def test_natural_approval_with_live_list_view_shape() -> None:
    """The pydantic ApprovalListView shape (.items as a list attribute) must
    not break approval capture."""
    gw = _ListViewApprovalGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")
    assert ctrl._waiting_approval_id == "appr-1"

    await ctrl.handle_input("да")
    assert gw.decide_calls == [("appr-1", True)]
    assert ctrl.state.terminal_outcome is not None
    assert ctrl.state.terminal_outcome.status is TerminalOutcomeStatus.SUCCESS


@pytest.mark.asyncio
async def test_task_like_phrase_gets_clarification_not_silent_chitchat() -> None:
    """Action phrases the server marks ambiguous ('write hello world') must
    get a clarification prompt — never silent chit-chat, never a false flow."""
    gw = _FakeGateway([FlowStatus.QUEUED])
    ctrl = _controller(gw)

    await ctrl.handle_input("write hello world")

    assert gw.submit_count == 0
    assert ctrl.state.terminal_outcome is None
    assert "Уточните" in ctrl.state.messages[-1].content


@pytest.mark.asyncio
async def test_ambiguous_verb_asks_clarification_without_flow() -> None:
    """'стоп' (server clarification) → clarification, never a task, never
    silent chit-chat."""
    gw = _FakeGateway([FlowStatus.QUEUED])
    ctrl = _controller(gw)

    await ctrl.handle_input("стоп")

    assert gw.submit_count == 0
    assert ctrl.state.terminal_outcome is None
    assert "Уточните" in ctrl.state.messages[-1].content


@pytest.mark.asyncio
async def test_approval_not_decided_for_unrelated_flow() -> None:
    """Fail-closed: when the pending approval belongs to another flow, 'да'
    must NOT decide it."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.DONE])
    gw.approvals = [_Approval("appr-other", "flow-OTHER")]
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    # No approval matches flow-123 → nothing captured, nothing decided.
    assert ctrl._waiting_approval_id is None
    await ctrl.handle_input("да")
    assert gw.decide_calls == []


@pytest.mark.asyncio
async def test_approval_capture_finds_flow_approval_beyond_foreign_entries() -> None:
    """The waiting flow's approval must be found even when other flows'
    pending approvals come first in the global list (live gateway has many
    stale PENDING approvals)."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL, FlowStatus.DONE])
    gw.approvals = [
        _Approval("appr-foreign-1", "flow-OTHER-1"),
        _Approval("appr-foreign-2", "flow-OTHER-2"),
        _Approval("appr-1", "flow-123"),
    ]
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    assert ctrl._waiting_approval_id == "appr-1"
    await ctrl.handle_input("да")
    assert gw.decide_calls == [("appr-1", True)]
    assert ctrl.state.terminal_outcome is not None
    assert ctrl.state.terminal_outcome.status is TerminalOutcomeStatus.SUCCESS


@pytest.mark.asyncio
async def test_gateway_unavailable_fails_closed() -> None:
    """Stage 1: if the Gateway/Turn API is unreachable the CLI fails CLOSED
    with an honest message — it must NOT start a local DialogueEngine and
    must NOT pretend the turn succeeded."""

    class _DownGateway:
        async def send_dialogue_turn(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("connection refused")

    ctrl = _controller(_DownGateway())

    await ctrl.handle_input("Привет!")

    assert ctrl.state.terminal_outcome is not None
    assert ctrl.state.terminal_outcome.status is TerminalOutcomeStatus.ERROR


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("да", True),
        ("да, разрешаю", True),
        ("нет", False),
        ("нет, отмени", False),
        ("да нет, погоди", False),  # denial wins
        ("не меняй конфигурацию", None),  # bare "не" is not a denial
        ("привет", None),
    ],
)
def test_approval_word_decision(text: str, expected: bool | None) -> None:
    assert chat_module._approval_word_decision(text) is expected
