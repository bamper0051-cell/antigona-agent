"""Typed outcome contract tests for the unified InputPipeline."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import antigona.input_pipeline.pipeline as pipeline_module
from antigona.core.control_plane import FlowStatus, FlowView
from antigona.core.gateway_client import GatewayTimeoutError, GatewayWaitTimeoutError
from antigona.input_pipeline.models import (
    ProcessingOutcome,
    ProcessingResult,
    UserInputEnvelope,
)
from antigona.input_pipeline.pipeline import process_user_input
from antigona.tasks.context_resolver import IntentType

_TERMINAL_STATUSES = frozenset(
    {
        FlowStatus.DONE,
        FlowStatus.FAILED,
        FlowStatus.BLOCKED,
        FlowStatus.CANCELLED,
        FlowStatus.TIMEOUT,
        FlowStatus.POLICY_DENIED,
    }
)
_NONTERMINAL_STATUSES = tuple(
    status for status in FlowStatus if status not in _TERMINAL_STATUSES
)
_UNAVAILABLE_RESULT_TEXT = (
    "Проверка завершена, но безопасный подтверждённый результат недоступен."
)


def _flow(
    status: FlowStatus,
    *,
    flow_id: str = "flow-1",
    result: str | None = None,
    error: str | None = None,
) -> FlowView:
    now = datetime.now(UTC).isoformat()
    return FlowView(
        flow_id=flow_id,
        conversation_id="chat-1",
        title="Test flow",
        status=status,
        progress=100 if status in _TERMINAL_STATUSES else 10,
        current_step=None,
        steps=[],
        events=[],
        result=result,
        error=error,
        created_at=now,
        updated_at=now,
    )


def _envelope() -> UserInputEnvelope:
    return UserInputEnvelope(
        source="telegram_text",
        user_id=42,
        chat_id=100,
        message_id=7,
        text="выполни задачу",
        correlation_id="corr-outcome-test",
    )


def _context(
    *,
    intent: IntentType = IntentType.NEW_TASK,
    task_id: str | None = None,
) -> MagicMock:
    context = MagicMock()
    context.intent = intent
    context.task_id = task_id
    context.steered_text = "выполни задачу"
    context.matched_task = MagicMock() if task_id else None
    context.confidence = 0.99
    return context


def _router(*, task: bool = True) -> MagicMock:
    router = MagicMock()
    router.route.return_value = SimpleNamespace(
        intent="task.shell" if task else "conversation.smalltalk",
        confidence=0.95,
        response_mode="task_preview" if task else "direct",
        requires_planner=task,
    )
    return router


def _gateway(
    *,
    accepted: FlowView | None = None,
    terminal: FlowView | None = None,
) -> AsyncMock:
    gateway = AsyncMock()
    gateway.submit = AsyncMock(return_value=accepted or _flow(FlowStatus.QUEUED))
    gateway.wait_for_terminal = AsyncMock(return_value=terminal)
    gateway.steer = AsyncMock(return_value=_flow(FlowStatus.RUNNING))
    gateway.cancel = AsyncMock(return_value=_flow(FlowStatus.RUNNING))
    return gateway


async def _run_new_task(gateway: AsyncMock) -> ProcessingResult:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=_context())
    return await process_user_input(
        envelope=_envelope(),
        intent_router=_router(),
        context_resolver=resolver,
        gateway_client=gateway,
    )


def test_processing_result_keeps_backwards_compatible_fail_closed_defaults() -> None:
    result = ProcessingResult(
        success=True,
        task_id="flow-1",
        session_id="flow-1",
        response_text="accepted",
        error=None,
        correlation_id="corr",
        duration_ms=1.0,
    )

    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False
    assert result.flow_status is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        pytest.param("-1", 0.1, id="minimum"),
        pytest.param("999", 300.0, id="maximum"),
        pytest.param("nan", 30.0, id="nan-falls-back"),
        pytest.param("not-a-number", 30.0, id="invalid-falls-back"),
    ],
)
def test_terminal_wait_configuration_is_finite_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    expected: float,
) -> None:
    monkeypatch.setenv("ANTIGONA_GATEWAY_TERMINAL_WAIT_SECONDS", configured)

    assert pipeline_module._terminal_wait_seconds() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", _NONTERMINAL_STATUSES, ids=lambda status: status.value)
async def test_every_nonterminal_gateway_status_remains_flow_accepted(
    status: FlowStatus,
) -> None:
    gateway = _gateway(terminal=_flow(status, result="must not be presented"))

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False
    assert result.flow_status is status
    assert result.response_text == "Задача принята и выполняется."
    assert result.error is None
    gateway.submit.assert_awaited_once()
    gateway.wait_for_terminal.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "terminal_result", "terminal_error", "expected_outcome", "expected_text"),
    [
        pytest.param(
            FlowStatus.DONE,
            "verified output",
            None,
            ProcessingOutcome.TERMINAL_SUCCESS,
            "verified output",
            id="done-with-safe-result",
        ),
        pytest.param(
            FlowStatus.DONE,
            None,
            None,
            ProcessingOutcome.TERMINAL_FAILURE,
            _UNAVAILABLE_RESULT_TEXT,
            id="done-without-result",
        ),
        pytest.param(
            FlowStatus.DONE,
            "   ",
            None,
            ProcessingOutcome.TERMINAL_FAILURE,
            _UNAVAILABLE_RESULT_TEXT,
            id="done-with-blank-result",
        ),
        pytest.param(
            FlowStatus.DONE,
            "untrusted output",
            "RuntimeError: database password=hunter2",
            ProcessingOutcome.TERMINAL_FAILURE,
            _UNAVAILABLE_RESULT_TEXT,
            id="done-with-error",
        ),
        pytest.param(
            FlowStatus.FAILED,
            None,
            "RuntimeError: database password=hunter2",
            ProcessingOutcome.TERMINAL_FAILURE,
            "Задача не выполнена.",
            id="failed",
        ),
        pytest.param(
            FlowStatus.FAILED,
            None,
            "mcp server 'edge-tts' is not registered (registered: context7)",
            ProcessingOutcome.TERMINAL_FAILURE,
            (
                "Задача не выполнена. Причина: mcp server &#x27;edge-tts&#x27; "
                "is not registered (registered: context7)"
            ),
            id="failed-with-specific-reason",
        ),
        pytest.param(
            FlowStatus.BLOCKED,
            None,
            "policy detail password=hunter2",
            ProcessingOutcome.TERMINAL_FAILURE,
            "Задача заблокирована политикой безопасности.",
            id="blocked",
        ),
        pytest.param(
            FlowStatus.TIMEOUT,
            None,
            "upstream token=topsecret",
            ProcessingOutcome.TERMINAL_FAILURE,
            "Задача завершилась по тайм-ауту.",
            id="timeout",
        ),
        pytest.param(
            FlowStatus.POLICY_DENIED,
            None,
            "raw policy internals password=hunter2",
            ProcessingOutcome.TERMINAL_FAILURE,
            "Выполнение отклонено политикой безопасности.",
            id="policy-denied",
        ),
        pytest.param(
            FlowStatus.CANCELLED,
            None,
            "raw cancellation diagnostic password=hunter2",
            ProcessingOutcome.CANCELLED,
            "Задача отменена.",
            id="cancelled",
        ),
    ],
)
async def test_every_terminal_status_has_typed_safe_semantics(
    status: FlowStatus,
    terminal_result: str | None,
    terminal_error: str | None,
    expected_outcome: ProcessingOutcome,
    expected_text: str,
) -> None:
    terminal = _flow(
        status,
        flow_id="untrusted-terminal-id",
        result=terminal_result,
        error=terminal_error,
    )
    gateway = _gateway(terminal=terminal)

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is expected_outcome
    assert result.terminal is True
    assert result.flow_status is status
    assert result.response_text == expected_text
    assert result.error is None
    public_text = f"{result.response_text or ''} {result.error or ''}"
    assert "RuntimeError" not in public_text
    assert "hunter2" not in public_text
    assert "topsecret" not in public_text


@pytest.mark.asyncio
async def test_wait_timeout_after_submit_is_nonterminal_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_GATEWAY_TERMINAL_WAIT_SECONDS", "0.25")
    gateway = _gateway()
    gateway.wait_for_terminal.side_effect = GatewayWaitTimeoutError("flow-1", 0.25)

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False
    assert result.flow_status is FlowStatus.QUEUED
    assert result.response_text == "Задача принята и выполняется."
    assert result.error is None
    gateway.wait_for_terminal.assert_awaited_once_with("flow-1", timeout=0.25)


@pytest.mark.asyncio
async def test_missing_wait_response_does_not_make_submit_response_final() -> None:
    gateway = _gateway(
        accepted=_flow(
            FlowStatus.DONE,
            result="untrusted submit response password=hunter2",
        ),
        terminal=None,
    )

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False
    assert result.flow_status is FlowStatus.DONE
    assert result.response_text == "Задача принята и выполняется."
    assert "hunter2" not in result.response_text
    gateway.wait_for_terminal.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_transport_timeout_is_safe_pipeline_failure() -> None:
    gateway = _gateway()
    gateway.submit.side_effect = GatewayTimeoutError(
        "upstream RuntimeError password=hunter2 token=topsecret"
    )

    result = await _run_new_task(gateway)

    assert result.success is False
    assert result.outcome is ProcessingOutcome.TERMINAL_FAILURE
    assert result.terminal is True
    assert result.flow_status is FlowStatus.FAILED
    assert result.response_text is None
    assert result.error == "Gateway не ответил вовремя."
    assert "RuntimeError" not in result.error
    assert "hunter2" not in result.error
    assert "topsecret" not in result.error
    gateway.wait_for_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_steering_acknowledgement_is_never_treated_as_task_final() -> None:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(
        return_value=_context(
            intent=IntentType.STEER_EXISTING,
            task_id="authoritative-task-id",
        )
    )
    gateway = _gateway()
    gateway.steer.return_value = _flow(
        FlowStatus.DONE,
        flow_id="untrusted-response-id",
        result="password=hunter2",
    )

    result = await process_user_input(
        envelope=_envelope(),
        intent_router=_router(),
        context_resolver=resolver,
        gateway_client=gateway,
    )

    assert result.success is True
    assert result.task_id == "authoritative-task-id"
    assert result.outcome is ProcessingOutcome.FLOW_STEERED
    assert result.terminal is False
    assert result.flow_status is FlowStatus.DONE
    assert result.response_text == "Задача скорректирована."
    assert "hunter2" not in result.response_text
    gateway.steer.assert_awaited_once()
    gateway.submit.assert_not_awaited()
    gateway.cancel.assert_not_awaited()
    gateway.wait_for_terminal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_status", "expected_outcome", "expected_terminal", "expected_text"),
    [
        pytest.param(
            FlowStatus.CANCELLED,
            ProcessingOutcome.CANCELLED,
            True,
            "Задача отменена.",
            id="authoritatively-cancelled",
        ),
        pytest.param(
            FlowStatus.RUNNING,
            ProcessingOutcome.FLOW_STEERED,
            False,
            "Запрос на отмену принят.",
            id="cancellation-request-only",
        ),
        pytest.param(
            FlowStatus.FAILED,
            ProcessingOutcome.FLOW_STEERED,
            False,
            "Запрос на отмену принят.",
            id="cancel-response-not-cancelled",
        ),
    ],
)
async def test_cancellation_uses_authoritative_input_task_id(
    cancel_status: FlowStatus,
    expected_outcome: ProcessingOutcome,
    expected_terminal: bool,
    expected_text: str,
) -> None:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(
        return_value=_context(
            intent=IntentType.CANCEL,
            task_id="authoritative-task-id",
        )
    )
    gateway = _gateway()
    gateway.cancel.return_value = _flow(
        cancel_status,
        flow_id="untrusted-response-id",
        error="RuntimeError password=hunter2",
    )

    result = await process_user_input(
        envelope=_envelope(),
        intent_router=_router(),
        context_resolver=resolver,
        gateway_client=gateway,
    )

    assert result.success is True
    assert result.task_id == "authoritative-task-id"
    assert result.outcome is expected_outcome
    assert result.terminal is expected_terminal
    assert result.flow_status is cancel_status
    assert result.response_text == expected_text
    assert result.error is None
    response_text = result.response_text
    assert response_text is not None
    assert "RuntimeError" not in response_text
    assert "hunter2" not in response_text
    gateway.cancel.assert_awaited_once_with(
        "authoritative-task-id",
        reason="user request via pipeline",
    )
    gateway.wait_for_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_untyped_cancel_mock_fields_cannot_replace_input_task_id() -> None:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(
        return_value=_context(
            intent=IntentType.CANCEL,
            task_id="authoritative-task-id",
        )
    )
    gateway = _gateway()
    gateway.cancel = AsyncMock()

    result = await process_user_input(
        envelope=_envelope(),
        intent_router=_router(),
        context_resolver=resolver,
        gateway_client=gateway,
    )

    assert result.success is True
    assert result.task_id == "authoritative-task-id"
    assert result.outcome is ProcessingOutcome.FLOW_STEERED
    assert result.terminal is False
    assert result.flow_status is None
    gateway.wait_for_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_result_is_redacted_again_at_pipeline_boundary() -> None:
    gateway = _gateway(
        terminal=_flow(
            FlowStatus.DONE,
            result="completed password=hunter2 token=topsecret",
        )
    )

    result = await _run_new_task(gateway)

    assert result.outcome is ProcessingOutcome.TERMINAL_SUCCESS
    assert result.terminal is True
    assert result.response_text is not None
    assert "completed" in result.response_text
    assert "[REDACTED]" in result.response_text
    assert "hunter2" not in result.response_text
    assert "topsecret" not in result.response_text


@pytest.mark.asyncio
async def test_done_traceback_is_not_misreported_as_terminal_success() -> None:
    gateway = _gateway(
        terminal=_flow(
            FlowStatus.DONE,
            result=(
                'Traceback (most recent call last):\n  File "worker.py", line 1\n'
                "RuntimeError: password=hunter2"
            ),
        )
    )

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.outcome is ProcessingOutcome.TERMINAL_FAILURE
    assert result.terminal is True
    assert result.response_text == _UNAVAILABLE_RESULT_TEXT
    assert result.error is None
    assert "Traceback" not in result.response_text
    assert "RuntimeError" not in result.response_text
    assert "hunter2" not in result.response_text


@pytest.mark.asyncio
async def test_conversation_without_gateway_flow_is_final_conversation_outcome() -> None:
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=_context())
    gateway = _gateway()

    result = await process_user_input(
        envelope=_envelope(),
        intent_router=_router(task=False),
        context_resolver=resolver,
        gateway_client=gateway,
    )

    assert result.success is True
    assert result.task_id is None
    assert result.response_text is None
    assert result.outcome is ProcessingOutcome.CONVERSATION_FINAL
    assert result.terminal is True
    assert result.flow_status is None
    gateway.submit.assert_not_awaited()
    gateway.wait_for_terminal.assert_not_awaited()


def test_pipeline_has_no_local_autonomous_loop_contour() -> None:
    tree = ast.parse(inspect.getsource(pipeline_module))

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "AutonomousLoop" not in imported
    assert "AutonomousLoop" not in referenced


# ── Stage 1 live-smoke regressions (2026-08-02) ──────────────────────────────


@pytest.mark.asyncio
async def test_waiting_approval_surfaces_without_burning_terminal_wait() -> None:
    """A flow that blocks on a human decision must return accepted NOW.

    Regression for the live Telegram smoke: a write-task went to
    WAITING_APPROVAL but the pipeline kept polling for the full 30s
    terminal-wait budget and only then reported "accepted", so the bot
    never rendered approve/reject buttons until the owner gave up.
    """
    gateway = _gateway()
    gateway.submit = AsyncMock(return_value=_flow(FlowStatus.QUEUED))
    gateway.get_flow = AsyncMock(
        side_effect=[
            _flow(FlowStatus.QUEUED),
            _flow(FlowStatus.WAITING_APPROVAL),
        ]
    )

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is ProcessingOutcome.FLOW_ACCEPTED
    assert result.terminal is False
    assert result.flow_status is FlowStatus.WAITING_APPROVAL
    assert result.response_text == "Задача принята и выполняется."
    # The canonical waiter must never be reached when the decision is surfaced.
    gateway.wait_for_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_result_still_wins_when_probe_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quick tasks that finish before the approval appears still report DONE.

    Ensures the approval-probe loop does not mask authoritative terminal
    results: when get_flow reports DONE, the terminal projection is used
    even though the flow was transient a moment earlier.
    """
    monkeypatch.setenv("ANTIGONA_GATEWAY_TERMINAL_WAIT_SECONDS", "0.5")
    gateway = _gateway()
    gateway.submit = AsyncMock(return_value=_flow(FlowStatus.QUEUED))
    gateway.get_flow = AsyncMock(
        return_value=_flow(
            FlowStatus.DONE,
            result="Готово: файл создан.",
        )
    )

    result = await _run_new_task(gateway)

    assert result.success is True
    assert result.task_id == "flow-1"
    assert result.outcome is ProcessingOutcome.TERMINAL_SUCCESS
    assert result.terminal is True
    assert result.flow_status is FlowStatus.DONE
    assert "Готово" in (result.response_text or "")
    gateway.wait_for_terminal.assert_not_awaited()
