"""Unified InputPipeline — normalise, resolve, route, submit.

Pipeline sequence:
  1. Normalise (from ``UserInputEnvelope``)
  2. Resolve context (``ContextResolver`` with reply / edit awareness)
  3. Classify intent (``IntentRouter``)
  4. Submit to Gateway (``GatewayClient.submit`` / ``.steer`` / ``.cancel``)
  5. Save Telegram message binding (``BindingRepository``)
  6. Return ``ProcessingResult``

The pipeline does **not** create a second Gateway or TaskRuntime — every
execution path goes through the existing ``GatewayClient``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from antigona.core.control_plane import (
    FlowStatus,
    FlowView,
    IntentClass,
    NormalizedRequest,
    SteeringCommand,
)
from antigona.core.gateway_client import (
    GatewayClient,
    GatewayConnectionError,
    GatewayError,
    GatewayHTTPError,
    GatewayTimeoutError,
    GatewayWaitTimeoutError,
)
from antigona.input_pipeline.binding_repository import BindingRepository
from antigona.input_pipeline.models import (
    ProcessingOutcome,
    ProcessingResult,
    UserInputEnvelope,
)
from antigona.input_pipeline.observability import (
    PIPELINE_EVENT_BINDING_SAVED,
    PIPELINE_EVENT_COMPLETED,
    PIPELINE_EVENT_CONTEXT_RESOLVED,
    PIPELINE_EVENT_GATEWAY_CANCEL,
    PIPELINE_EVENT_GATEWAY_STEER,
    PIPELINE_EVENT_GATEWAY_SUBMIT,
    PIPELINE_EVENT_INTENT_CLASSIFIED,
    PIPELINE_EVENT_RECEIVED,
    log_result,
    log_stage,
    publish_pipeline_event,
)
from antigona.result_safety import (
    DIAGNOSTIC_OMITTED,
    public_failure_reason,
    sanitize_result_text,
)
from antigona.router.intent_router import IntentDecision, IntentRouter
from antigona.tasks.context_resolver import ContextResolver

logger = logging.getLogger(__name__)

_DEFAULT_TERMINAL_WAIT_SECONDS = 30.0
_MAX_TERMINAL_WAIT_SECONDS = 300.0
_FAILURE_FLOW_STATUSES = frozenset(
    {
        FlowStatus.FAILED,
        FlowStatus.BLOCKED,
        FlowStatus.TIMEOUT,
        FlowStatus.POLICY_DENIED,
    }
)
_TERMINAL_FLOW_STATUSES = _FAILURE_FLOW_STATUSES | {
    FlowStatus.DONE,
    FlowStatus.CANCELLED,
}

#: Statuses a freshly submitted flow passes through before either reaching a
#: human decision (WAITING_APPROVAL / WAITING_USER) or a terminal outcome.
#: The approval-probe loop polls only these transient statuses for a short
#: window, then falls back to the canonical ``wait_for_terminal``.
_TRANSIENT_FLOW_STATUSES = frozenset(
    {
        FlowStatus.CREATED,
        FlowStatus.RECEIVED,
        FlowStatus.QUEUED,
        FlowStatus.READY,
        FlowStatus.PLANNING,
        FlowStatus.RUNNING,
        FlowStatus.TOOL_EXECUTING,
        FlowStatus.OBSERVING,
        FlowStatus.VERIFYING,
        FlowStatus.RETRY_SCHEDULED,
        FlowStatus.REPLAN_REQUESTED,
    }
)


@dataclass(frozen=True, slots=True)
class _DispatchResult:
    task_id: str | None
    response_text: str | None
    outcome: ProcessingOutcome
    terminal: bool
    flow_status: FlowStatus | None = None


def _terminal_wait_seconds() -> float:
    raw = os.getenv(
        "ANTIGONA_GATEWAY_TERMINAL_WAIT_SECONDS",
        str(_DEFAULT_TERMINAL_WAIT_SECONDS),
    )
    try:
        configured = float(raw)
    except ValueError:
        configured = _DEFAULT_TERMINAL_WAIT_SECONDS
    if not math.isfinite(configured):
        configured = _DEFAULT_TERMINAL_WAIT_SECONDS
    return max(0.1, min(configured, _MAX_TERMINAL_WAIT_SECONDS))


async def process_user_input(
    envelope: UserInputEnvelope,
    intent_router: IntentRouter,
    context_resolver: ContextResolver,
    gateway_client: GatewayClient,
    event_bus: Any | None = None,
    binding_repository: BindingRepository | None = None,
) -> ProcessingResult:
    """Process a single user input through the full pipeline.

    Args:
        envelope: Normalised input envelope from any channel.
        intent_router: Intent classifier.
        context_resolver: Context resolver (reply/edit/task resolution).
        gateway_client: HTTP client to the Gateway service.
        event_bus: Optional EventBus for pipeline lifecycle events.
        binding_repository: Optional repository for Telegram message bindings.
            **Required** when ``envelope.source`` starts with ``"telegram"``.

    Returns:
        A ``ProcessingResult`` summarising the outcome.
    """
    start = time.monotonic()
    correlation_id = envelope.correlation_id
    task_id: str | None = None

    # ── Stage 0: Receive ────────────────────────────────────────────────
    log_stage(PIPELINE_EVENT_RECEIVED, envelope)
    await publish_pipeline_event(event_bus, PIPELINE_EVENT_RECEIVED, envelope)

    try:
        # ── Stage 1: Context resolution ─────────────────────────────────
        ctx = await context_resolver.resolve(
            text=envelope.text,
            chat_id=envelope.chat_id,
            user_id=envelope.user_id,
            message_id=envelope.message_id,
            reply_to_message_id=envelope.reply_to_message_id,
            edited_message_id=envelope.edited_message_id,
        )
        log_stage(PIPELINE_EVENT_CONTEXT_RESOLVED, envelope, extra={
            "intent": ctx.intent.value,
            "task_id": ctx.task_id[:12] if ctx.task_id else None,
            "confidence": round(ctx.confidence, 3),
        })

        # ── Stage 2: Intent classification via IntentRouter ──────────────
        decision = _classify_intent(envelope, ctx, intent_router)
        log_stage(PIPELINE_EVENT_INTENT_CLASSIFIED, envelope, extra={
            "router_intent": decision.intent,
            "confidence": round(decision.confidence, 3),
            "response_mode": decision.response_mode,
        })
        await publish_pipeline_event(event_bus, PIPELINE_EVENT_INTENT_CLASSIFIED, envelope)

        # ── Stage 3: Gateway interaction ─────────────────────────────────
        dispatch = _DispatchResult(
            task_id=ctx.task_id or None,
            response_text=None,
            outcome=ProcessingOutcome.CONVERSATION_FINAL,
            terminal=True,
        )

        # ContextResolver result takes priority: if it found an existing task
        # with a control or steering intent, route accordingly.
        from antigona.tasks.context_resolver import IntentType

        if ctx.task_id and ctx.intent in (
            IntentType.CANCEL,
            IntentType.PAUSE,
            IntentType.RESUME,
            IntentType.RETRY,
        ):
            task_id = ctx.task_id
            if ctx.intent == IntentType.CANCEL:
                dispatch = await _gateway_cancel(
                    ctx.task_id,
                    envelope,
                    gateway_client,
                    event_bus,
                )
            else:
                dispatch = await _gateway_steer(
                    ctx.task_id,
                    envelope,
                    ctx,
                    gateway_client,
                    event_bus,
                )

        elif ctx.task_id and ctx.intent in (
            IntentType.STEER_EXISTING,
            IntentType.CORRECT_MESSAGE,
            IntentType.ADD_REQUIREMENT,
            IntentType.ANSWER,
            IntentType.CONFIRM,
        ):
            task_id = ctx.task_id
            dispatch = await _gateway_steer(
                ctx.task_id,
                envelope,
                ctx,
                gateway_client,
                event_bus,
            )

        elif decision.intent.startswith("task.") or decision.requires_planner:
            dispatch = await _gateway_submit(
                envelope,
                ctx,
                decision,
                gateway_client,
                event_bus,
            )
            task_id = dispatch.task_id
        else:
            task_id = ctx.task_id or None

        task_id = dispatch.task_id or task_id

        # ── Stage 4: Save binding ───────────────────────────────────────
        if envelope.source.startswith("telegram") and binding_repository is not None:
            await _save_binding(
                envelope, correlation_id, task_id, binding_repository, event_bus,
            )

        # ── Result ──────────────────────────────────────────────────────
        duration_ms = (time.monotonic() - start) * 1000
        result = ProcessingResult(
            success=True,
            task_id=task_id,
            session_id=ctx.task_id or task_id,
            response_text=dispatch.response_text,
            error=None,
            correlation_id=correlation_id,
            duration_ms=round(duration_ms, 3),
            outcome=dispatch.outcome,
            terminal=dispatch.terminal,
            flow_status=dispatch.flow_status,
        )
        await publish_pipeline_event(event_bus, PIPELINE_EVENT_COMPLETED, envelope)
        log_result(result)
        return result

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        error_msg = _public_pipeline_error(exc)
        logger.exception("Pipeline failed for correlation_id=%s", correlation_id[:12])

        result = ProcessingResult(
            success=False,
            task_id=task_id,
            session_id=task_id,
            response_text=None,
            error=error_msg,
            correlation_id=correlation_id,
            duration_ms=round(duration_ms, 3),
            outcome=ProcessingOutcome.TERMINAL_FAILURE,
            terminal=True,
            flow_status=FlowStatus.FAILED,
        )
        await publish_pipeline_event(event_bus, PIPELINE_EVENT_COMPLETED, envelope)
        log_result(result)
        return result


# ── Internal helpers ──────────────────────────────────────────────────────────


def _classify_intent(
    envelope: UserInputEnvelope,
    ctx: Any,
    intent_router: IntentRouter,
) -> IntentDecision:
    """Run the input through IntentRouter and return an IntentDecision.

    Uses ``ctx`` (ResolvedContext) to build conversation history context
    for the router.
    """
    context: dict[str, Any] = {
        "correlation_id": envelope.correlation_id,
        "source": envelope.source,
    }
    if ctx.matched_task:
        context["active_task_id"] = ctx.task_id

    return intent_router.route(text=envelope.text, context=context)


def _coerce_flow_status(value: object) -> FlowStatus | None:
    if isinstance(value, FlowStatus):
        return value
    try:
        return FlowStatus(str(value).upper())
    except ValueError:
        return None


def _accepted_dispatch(
    flow_id: str,
    *,
    outcome: ProcessingOutcome,
    text: str,
    status: FlowStatus | None,
) -> _DispatchResult:
    return _DispatchResult(
        task_id=flow_id,
        response_text=text,
        outcome=outcome,
        terminal=False,
        flow_status=status,
    )


async def _wait_for_authoritative_terminal(
    gateway_client: GatewayClient,
    accepted: FlowView,
    *,
    accepted_outcome: ProcessingOutcome,
    accepted_text: str,
) -> _DispatchResult:
    """Map terminal projection or retain a nonterminal accepted outcome."""
    flow_id = accepted.flow_id
    accepted_status = _coerce_flow_status(accepted.status)
    # Stage 1 fix (live smoke, 2026-08-02): a flow that blocks on a human
    # decision (WAITING_APPROVAL / WAITING_USER) must NOT burn the full
    # terminal-wait budget — the client needs the approval surfaced NOW so it
    # can render approve/reject buttons. Poll the flow with a short interval
    # (same pattern as the CLI wait loop); as soon as it blocks on a decision,
    # return the accepted outcome with the real status instead of waiting out
    # the 30s terminal timeout. The approval is created asynchronously right
    # after submit, so a single probe is not enough — keep polling until the
    # flow leaves the transient RUNNING/QUEUED states.
    wait_seconds = _terminal_wait_seconds()
    poll_interval = 0.5
    # Short bounded window (seconds) in which we poll for the async
    # approval to appear before falling back to the canonical waiter.
    approval_probe_window = min(5.0, wait_seconds)
    approval_deadline = time.monotonic() + approval_probe_window
    probe: FlowView | None = None
    probe_status: FlowStatus | None = accepted_status
    while True:
        try:
            probe = await gateway_client.get_flow(flow_id)
            probe_status = _coerce_flow_status(probe.status)
        except Exception:
            probe_status = None
        if probe_status in {FlowStatus.WAITING_APPROVAL, FlowStatus.WAITING_USER}:
            logger.info(
                "Flow %s blocks on a human decision (%s); surfacing approval now",
                flow_id[:12],
                probe_status.value if probe_status is not None else "unknown",
            )
            return _accepted_dispatch(
                flow_id,
                outcome=accepted_outcome,
                text=accepted_text,
                status=probe_status,
            )
        if probe_status in _TERMINAL_FLOW_STATUSES and probe is not None:
            return _project_terminal_flow(probe, task_id=flow_id)
        # Only keep polling while the flow is in a transient pre-decision
        # state. If the probe returns something unknown/unusable (or the
        # window expires), fall through to the canonical waiter so tests and
        # callers that mock only ``wait_for_terminal`` keep working.
        if probe_status not in _TRANSIENT_FLOW_STATUSES:
            break
        if time.monotonic() >= approval_deadline:
            break
        await asyncio.sleep(poll_interval)

    try:
        terminal = await gateway_client.wait_for_terminal(
            flow_id,
            timeout=wait_seconds,
        )
    except GatewayError as exc:
        logger.warning(
            "Gateway terminal observation unavailable for flow %s (%s)",
            flow_id[:12],
            type(exc).__name__,
        )
        return _accepted_dispatch(
            flow_id,
            outcome=accepted_outcome,
            text=accepted_text,
            status=probe_status or accepted_status,
        )
    except Exception as exc:
        logger.warning(
            "Gateway terminal observation failed for flow %s (%s)",
            flow_id[:12],
            type(exc).__name__,
        )
        return _accepted_dispatch(
            flow_id,
            outcome=accepted_outcome,
            text=accepted_text,
            status=probe_status or accepted_status,
        )

    if terminal is None:
        return _accepted_dispatch(
            flow_id,
            outcome=accepted_outcome,
            text=accepted_text,
            status=probe_status or accepted_status,
        )
    status = _coerce_flow_status(terminal.status)
    if status not in _TERMINAL_FLOW_STATUSES:
        return _accepted_dispatch(
            flow_id,
            outcome=accepted_outcome,
            text=accepted_text,
            status=status,
        )
    return _project_terminal_flow(terminal, task_id=flow_id)


def _project_terminal_flow(
    flow: FlowView,
    *,
    task_id: str | None = None,
) -> _DispatchResult:
    status = _coerce_flow_status(flow.status)
    authoritative_task_id = task_id or flow.flow_id
    if status is FlowStatus.DONE:
        safe_text = sanitize_result_text(flow.result, max_length=3500)
        if (
            safe_text
            and safe_text.strip()
            and safe_text != DIAGNOSTIC_OMITTED
            and not flow.error
        ):
            return _DispatchResult(
                task_id=authoritative_task_id,
                response_text=safe_text,
                outcome=ProcessingOutcome.TERMINAL_SUCCESS,
                terminal=True,
                flow_status=status,
            )
        return _DispatchResult(
            task_id=authoritative_task_id,
            response_text=(
                "Проверка завершена, но безопасный подтверждённый "
                "результат недоступен."
            ),
            outcome=ProcessingOutcome.TERMINAL_FAILURE,
            terminal=True,
            flow_status=status,
        )

    if status is FlowStatus.CANCELLED:
        return _DispatchResult(
            task_id=authoritative_task_id,
            response_text="Задача отменена.",
            outcome=ProcessingOutcome.CANCELLED,
            terminal=True,
            flow_status=status,
        )

    if status is FlowStatus.BLOCKED:
        failure_text = "Задача заблокирована политикой безопасности."
    elif status is FlowStatus.TIMEOUT:
        failure_text = "Задача завершилась по тайм-ауту."
    elif status is FlowStatus.POLICY_DENIED:
        failure_text = "Выполнение отклонено политикой безопасности."
    else:
        # The generic branch is exactly where the bare, content-free constant
        # appeared. Interpolate the bounded, sanitized REAL reason (redacted,
        # never raw exception/stack text, never internal fencing wording) so
        # the owner learns WHY instead of "Задача не выполнена.".
        reason = public_failure_reason(flow.error)
        failure_text = f"Задача не выполнена. Причина: {reason}" if reason else "Задача не выполнена."
    return _DispatchResult(
        task_id=authoritative_task_id,
        response_text=failure_text,
        outcome=ProcessingOutcome.TERMINAL_FAILURE,
        terminal=True,
        flow_status=status,
    )


def _public_pipeline_error(exc: Exception) -> str:
    if isinstance(exc, GatewayConnectionError):
        return "Gateway временно недоступен."
    if isinstance(exc, (GatewayWaitTimeoutError, GatewayTimeoutError)):
        return "Gateway не ответил вовремя."
    if isinstance(exc, GatewayHTTPError):
        return "Gateway отклонил запрос."
    if isinstance(exc, GatewayError):
        return "Не удалось выполнить запрос через Gateway."
    return "Не удалось безопасно обработать запрос."


async def _gateway_submit(
    envelope: UserInputEnvelope,
    ctx: Any,
    decision: IntentDecision,
    gateway_client: GatewayClient,
    event_bus: Any | None,
) -> _DispatchResult:
    """Submit once, then wait boundedly for the authoritative Gateway state."""
    intent_class = _resolve_intent_class(decision.intent)
    request = NormalizedRequest(
        source=envelope.source,
        correlation_id=envelope.correlation_id,
        conversation_id=str(envelope.chat_id),
        owner_id=str(envelope.user_id),
        user_message=ctx.steered_text or envelope.text,
        message_id=envelope.message_id,
        reply_to_message_id=envelope.reply_to_message_id,
        edited_message_id=envelope.edited_message_id,
        intent=intent_class,
        intent_confidence=decision.confidence,
        metadata={
            "source": envelope.source,
            "correlation_id": envelope.correlation_id,
            "response_mode": decision.response_mode,
            **(envelope.metadata or {}),
        },
    )

    accepted = await gateway_client.submit(request)
    await publish_pipeline_event(event_bus, PIPELINE_EVENT_GATEWAY_SUBMIT, envelope)
    return await _wait_for_authoritative_terminal(
        gateway_client,
        accepted,
        accepted_outcome=ProcessingOutcome.FLOW_ACCEPTED,
        accepted_text="Задача принята и выполняется.",
    )


async def _gateway_steer(
    task_id: str,
    envelope: UserInputEnvelope,
    ctx: Any,
    gateway_client: GatewayClient,
    event_bus: Any | None,
) -> _DispatchResult:
    """A steering response is an acknowledgement, never a task final."""
    command = SteeringCommand(
        flow_id=task_id,
        command="modify",
        modification_text=ctx.steered_text or envelope.text,
        correlation_id=envelope.correlation_id,
    )
    flow = await gateway_client.steer(task_id, command)
    await publish_pipeline_event(event_bus, PIPELINE_EVENT_GATEWAY_STEER, envelope)
    return _DispatchResult(
        task_id=task_id,
        response_text="Задача скорректирована.",
        outcome=ProcessingOutcome.FLOW_STEERED,
        terminal=False,
        flow_status=_coerce_flow_status(getattr(flow, "status", None)),
    )


async def _gateway_cancel(
    task_id: str,
    envelope: UserInputEnvelope,
    gateway_client: GatewayClient,
    event_bus: Any | None,
) -> _DispatchResult:
    """Cancel explicitly, but report CANCELLED only after terminal evidence."""
    response = await gateway_client.cancel(
        task_id,
        reason="user request via pipeline",
    )
    await publish_pipeline_event(event_bus, PIPELINE_EVENT_GATEWAY_CANCEL, envelope)
    status = _coerce_flow_status(getattr(response, "status", None))
    if status is FlowStatus.CANCELLED:
        return _DispatchResult(
            task_id=task_id,
            response_text="Задача отменена.",
            outcome=ProcessingOutcome.CANCELLED,
            terminal=True,
            flow_status=status,
        )
    return _accepted_dispatch(
        task_id,
        outcome=ProcessingOutcome.FLOW_STEERED,
        text="Запрос на отмену принят.",
        status=status,
    )


async def _save_binding(
    envelope: UserInputEnvelope,
    correlation_id: str,
    task_id: str | None,
    binding_repository: BindingRepository,
    event_bus: Any | None,
) -> None:
    """Persist a Telegram message binding.

    This is a best-effort operation — failures log a warning but never
    bubble up to the caller.
    """
    try:
        await binding_repository.save(
            chat_id=envelope.chat_id,
            telegram_message_id=envelope.message_id,
            user_id=envelope.user_id if envelope.user_id else None,
            task_id=task_id,
            correlation_id=correlation_id,
            message_role="user",
            message_kind=_resolve_message_kind(envelope.source),
            original_text=envelope.text,
            metadata_json=envelope.metadata or {},
        )
        await publish_pipeline_event(
            event_bus, PIPELINE_EVENT_BINDING_SAVED, envelope,
        )
    except Exception:
        logger.warning(
            "Failed to save binding for corr=%s chat=%d msg=%d",
            correlation_id[:12],
            envelope.chat_id,
            envelope.message_id,
            exc_info=True,
        )


def _resolve_message_kind(source: str) -> str:
    """Map envelope source to a ``message_kind`` value."""
    kind_map = {
        "telegram_voice": "voice",
        "telegram_edited": "text",
    }
    return kind_map.get(source, "text")


def _resolve_intent_class(intent: str) -> IntentClass | None:
    """Map an ``IntentDecision.intent`` string to an ``IntentClass`` enum.

    Falls back to ``None`` for conversational intents.
    """
    routing_map: dict[str, IntentClass] = {
        "task.file_write": IntentClass.TASK_CREATE,
        "task.file_edit": IntentClass.TASK_CREATE,
        "task.shell": IntentClass.TASK_CREATE,
        "task.code_change": IntentClass.TASK_CREATE,
        "command.status": IntentClass.FLOW_STATUS,
        "command.cancel": IntentClass.TASK_CANCEL,
        "command.resume": IntentClass.TASK_RESUME,
    }
    return routing_map.get(intent)


# ── Restart recovery ──────────────────────────────────────────────────────────


async def recover_context_after_restart(
    chat_id: int,
    binding_repository: BindingRepository,
    context_resolver: ContextResolver,
) -> dict[str, int]:
    """Восстановить контекст после перезапуска для одного чата.

    Загружает последние bindings из БД и пробует восстановить
    task → session связь через ContextResolver.

    Args:
        chat_id: Telegram chat ID.
        binding_repository: BindingRepository для загрузки bindings.
        context_resolver: ContextResolver для восстановления task-связей.

    Returns:
        Словарь со статистикой восстановления:
        ``{"bindings_loaded": N, "tasks_recovered": M}``.
    """
    bindings = await binding_repository.load_chat_bindings(chat_id, limit=20)

    recovered_task_ids: set[str] = set()
    for binding in bindings:
        if binding.task_id and binding.original_text:
            # Пробуем восстановить контекст через resolve
            try:
                ctx = await context_resolver.resolve(
                    text=binding.original_text,
                    chat_id=chat_id,
                    user_id=binding.user_id or 0,
                    message_id=binding.telegram_message_id,
                )
                if ctx.task_id:
                    recovered_task_ids.add(ctx.task_id)
            except Exception:
                logger.debug(
                    "Context recovery skipped for chat=%d msg=%d",
                    chat_id,
                    binding.telegram_message_id,
                )

    return {
        "bindings_loaded": len(bindings),
        "tasks_recovered": len(recovered_task_ids),
    }


async def recover_all_contexts(
    binding_repository: BindingRepository,
    context_resolver: ContextResolver,
    *,
    chat_ids: list[int] | None = None,
) -> dict[int, dict[str, int]]:
    """Восстановить контекст после перезапуска для всех (или указанных) чатов.

    Args:
        binding_repository: BindingRepository для загрузки bindings.
        context_resolver: ContextResolver для восстановления task-связей.
        chat_ids: Список chat_id для восстановления. Если None — загружаются
            все bindings и определяются уникальные chat_id.

    Returns:
        Словарь ``{chat_id: {"bindings_loaded": N, "tasks_recovered": M}}``.
    """
    if chat_ids is None:
        # Собираем уникальные chat_id из последних bindings
        all_bindings = await binding_repository.load_all_bindings(limit=500)
        chat_ids = list({b.chat_id for b in all_bindings})

    results: dict[int, dict[str, int]] = {}
    for cid in chat_ids:
        try:
            results[cid] = await recover_context_after_restart(
                cid,
                binding_repository,
                context_resolver,
            )
        except Exception:
            logger.exception("Failed to recover context for chat=%d", cid)
            results[cid] = {"bindings_loaded": 0, "tasks_recovered": 0}

    return results
