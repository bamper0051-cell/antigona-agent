"""Regression tests: `antigona chat` free text must feel live again.

After submission the controller must KEEP watching the flow (poll) instead of
printing "Task created... QUEUED" and dropping the user back to the prompt.
When the flow blocks on a human decision the approval list is rendered; after
``/approve``/``/deny`` the controller resumes watching the same flow until it
reaches a validated terminal outcome.

Regression origin: commit 74da4b59 removed the wait loop; commit 57035d60
re-added submission via POST /tasks but never re-attached the wait.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from antigona.cli_ui.chat import ChatController
from antigona.cli_ui.models import TerminalOutcomeStatus
from antigona.core.control_plane import FlowStatus


class _FlowView:
    def __init__(self, status: FlowStatus, result: Any = None) -> None:
        self.status = status
        self.result = result
        self.result_data = result
        self.error = None
        self.error_message = None


class _Approval:
    def __init__(self, aid: str, task_id: str) -> None:
        self.id = aid
        self.task_id = task_id
        self.tool_name = "workspace.write_text"
        self.risk_level = "sensitive"
        self.reason = "approval required"


class _FakeGateway:
    """Scripted gateway: send_dialogue_turn emulates the server brain (task
    phrases → task_accepted + flow_id); get_flow replays a status script;
    decide_approval forces the flow to DONE (approve) or FAILED (deny)."""

    def __init__(
        self,
        script: list[FlowStatus],
        flow_id: str = "flow-123",
        result_text: str | None = None,
    ) -> None:
        self._script = list(script)
        self._idx = 0
        self.flow_id = flow_id
        self.other_flows: dict[str, _FlowView] = {}
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
        """Emulate the server brain: task phrases → task_accepted + flow_id."""
        self.turn_calls.append(text)
        return {
            "reply": "Задача принята и выполняется.",
            "session_id": session_id,
            "verified": None,
            "response_type": "task_accepted",
            "flow_id": self.flow_id,
            "requires_approval": True,
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
        if flow_id in self.other_flows:
            return self.other_flows[flow_id]
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
        if flow_id in self.other_flows:
            return self.other_flows[flow_id]
        return _FlowView(FlowStatus.DONE, result=self.result_text)

    async def close(self) -> Any:
        return None


class _NonCanonicalGateway:
    """Implements GatewayClientProtocol but NOT TerminalWaiterProtocol:
    no wait_for_terminal method at all, so isinstance(x, TerminalWaiterProtocol)
    is False and the canonical success gate cannot be opened."""

    def __init__(self, script: list[FlowStatus]) -> None:
        self._script = list(script)
        self._idx = 0
        self.submit_count = 0
        self.turn_calls: list[str] = []
        self.approvals = [_Approval("appr-1", "flow-123")]

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
            "flow_id": "flow-123",
            "requires_approval": True,
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
        return {"id": "flow-123", "status": "QUEUED"}

    async def get_flow(self, flow_id: str) -> _FlowView:
        status = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1
        return _FlowView(status)

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
        return None

    async def close(self) -> Any:
        return None


def _controller(gw: Any, **kw: Any) -> ChatController:
    return ChatController(gateway=gw, renderer=None, poll_interval_sec=0.001, **kw)


@pytest.mark.asyncio
async def test_free_text_submission_waits_until_done() -> None:
    """Free text must NOT return after QUEUED — it polls to a validated DONE."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.RUNNING, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    assert gw.turn_calls == ["создай файл test.txt"]
    assert gw.submit_count == 0  # задача создаётся серверным ядром, не CLI
    # The controller actually watched the flow (polled get_flow more than once).
    assert gw._idx >= 3, "controller returned without polling the flow to DONE"
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.SUCCESS


@pytest.mark.asyncio
async def test_free_text_shows_approval_then_approve_resumes_and_completes() -> None:
    """WAITING_APPROVAL renders the approval list and returns control; after
    /approve the controller resumes watching and surfaces the validated result."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    assert gw.turn_calls == ["создай файл test.txt"]
    assert ctrl.state.terminal_outcome is None  # not terminal yet
    assert any("appr-1" in m.content for m in ctrl.state.messages), (
        "approval list was not rendered when the flow blocked"
    )
    assert ctrl.state.current_status == "waiting_approval"

    await ctrl.handle_input("/approve appr-1")

    assert gw.decide_calls == [("appr-1", True)]
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None, "/approve did not resume watching the flow"
    assert outcome.status is TerminalOutcomeStatus.SUCCESS
    assert outcome.result_data == "done-result"


@pytest.mark.asyncio
async def test_deny_sends_decision_and_ends_non_success() -> None:
    """/deny sends the decision; a denied flow must NOT present SUCCESS."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")
    await ctrl.handle_input("/deny appr-1")

    assert gw.decide_calls == [("appr-1", False)]
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is not TerminalOutcomeStatus.SUCCESS
    assert not outcome.is_success()


@pytest.mark.asyncio
async def test_non_canonical_client_never_opens_success_gate() -> None:
    """A bare get_flow DONE on a client without wait_for_terminal must be
    refused (MALFORMED), mirroring _poll_without_canonical_waiter."""
    gw = _NonCanonicalGateway([FlowStatus.QUEUED, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")

    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.MALFORMED
    assert not outcome.is_success()


@pytest.mark.asyncio
async def test_stale_cancel_event_does_not_disable_waiting() -> None:
    """After a previous cancel_local_wait(), the next free-text message must
    still poll and complete (each wait starts with a fresh cancel event)."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.RUNNING, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.cancel_local_wait()
    await ctrl.handle_input("создай файл test.txt")

    assert gw._idx >= 3, "stale cancel event disabled the live wait"
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.SUCCESS


@pytest.mark.asyncio
async def test_approve_resumes_waiting_flow_not_navigated_flow() -> None:
    """/approve must resume the flow that was actually waiting, even if the
    user navigated to another flow with /status in between."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.WAITING_APPROVAL])
    gw.other_flows["flow-999"] = _FlowView(FlowStatus.DONE, result="result-of-flow-B")
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")  # waits on flow-123 → WAITING_APPROVAL
    await ctrl.handle_input("/status flow-999")  # navigates; active_flow_id = flow-999
    assert ctrl.active_flow_id == "flow-999"
    await ctrl.handle_input("/approve appr-1")  # must resume flow-123, NOT flow-999

    assert gw.decide_calls == [("appr-1", True)]
    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.SUCCESS
    assert outcome.result_data == "done-result"  # flow-123 result, not flow-999


@pytest.mark.asyncio
async def test_wait_times_out_with_bounded_deadline() -> None:
    """A flow that never reaches a terminal state yields TIMEOUT, bounded by
    max_poll_sec, not an unbounded hang."""
    gw = _FakeGateway([FlowStatus.QUEUED])
    ctrl = _controller(gw, max_poll_sec=0.05)

    await ctrl.handle_input("создай файл test.txt")

    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.TIMEOUT


class _RaisingWaiterGateway(_FakeGateway):
    """Canonical-looking client whose wait_for_terminal blows up — must not
    kill the session (P1-1)."""

    async def wait_for_terminal(
        self,
        flow_id: str,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        cancel_event: Any = None,
    ) -> _FlowView:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_wait_validation_failure_renders_error_and_keeps_session() -> None:
    """A wait_for_terminal exception must render a redacted ERROR outcome and
    return normally — handle_input must not propagate the exception."""
    gw = _RaisingWaiterGateway([FlowStatus.QUEUED, FlowStatus.DONE])
    ctrl = _controller(gw)

    await ctrl.handle_input("создай файл test.txt")  # must not raise

    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.ERROR
    assert "boom" not in (outcome.error_message or "")


@pytest.mark.asyncio
async def test_sigint_during_wait_cancels_wait_and_keeps_session() -> None:
    """Ctrl+C (simulated via the signal-handler target) during a live wait
    cancels the wait with a CANCELLED outcome; the session survives."""
    gw = _FakeGateway([FlowStatus.QUEUED, FlowStatus.QUEUED, FlowStatus.QUEUED])
    ctrl = _controller(gw, max_poll_sec=5.0)

    task = asyncio.create_task(ctrl.handle_input("создай файл test.txt"))
    await asyncio.sleep(0.05)  # let the wait start polling
    ctrl._cancel_current_wait()  # same code path as the SIGINT handler
    await task

    outcome = ctrl.state.terminal_outcome
    assert outcome is not None
    assert outcome.status is TerminalOutcomeStatus.CANCELLED
