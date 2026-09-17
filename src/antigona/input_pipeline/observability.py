"""Observability helpers for the input pipeline.

Provides structured logging and EventBus events for each pipeline stage.
All functions accept a ``correlation_id`` and ``duration_ms`` for end-to-end
traceability.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from antigona.input_pipeline.models import ProcessingResult, UserInputEnvelope

logger = logging.getLogger(__name__)

# ── Pipeline-stage event types (published on EventBus) ──────────────────────

PIPELINE_EVENT_RECEIVED = "PIPELINE_RECEIVED"
"""Input envelope received (immediately after entering the pipeline)."""

PIPELINE_EVENT_CONTEXT_RESOLVED = "PIPELINE_CONTEXT_RESOLVED"
"""ContextResolver returned a ResolvedContext."""

PIPELINE_EVENT_INTENT_CLASSIFIED = "PIPELINE_INTENT_CLASSIFIED"
"""IntentRouter returned an IntentDecision."""

PIPELINE_EVENT_GATEWAY_SUBMIT = "PIPELINE_GATEWAY_SUBMIT"
"""GatewayClient.submit called for a new task."""

PIPELINE_EVENT_GATEWAY_STEER = "PIPELINE_GATEWAY_STEER"
"""GatewayClient.steer called for an existing task."""

PIPELINE_EVENT_GATEWAY_CANCEL = "PIPELINE_GATEWAY_CANCEL"
"""GatewayClient.cancel called."""

PIPELINE_EVENT_BINDING_SAVED = "PIPELINE_BINDING_SAVED"
"""TelegramMessageBinding persisted."""

PIPELINE_EVENT_COMPLETED = "PIPELINE_COMPLETED"
"""Pipeline finished (success or error)."""


# ── Structured-log helpers ──────────────────────────────────────────────────


def log_stage(
    stage: str,
    envelope: UserInputEnvelope,
    *,
    duration_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a structured log line for a pipeline stage."""
    fields: dict[str, Any] = {
        "pipeline_stage": stage,
        "correlation_id": envelope.correlation_id,
        "source": envelope.source,
        "chat_id": envelope.chat_id,
        "user_id": envelope.user_id,
        "message_id": envelope.message_id,
    }
    if duration_ms is not None:
        fields["duration_ms"] = round(duration_ms, 3)
    if extra:
        fields.update(extra)

    logger.info(
        "Pipeline[%s] corr=%s source=%s chat=%d msg=%d%s",
        stage,
        envelope.correlation_id[:12],
        envelope.source,
        envelope.chat_id,
        envelope.message_id,
        f"  dur={duration_ms:.1f}ms" if duration_ms is not None else "",
    )


# ── EventBus helpers ────────────────────────────────────────────────────────


async def publish_pipeline_event(
    event_bus: Any | None,
    event_type: str,
    envelope: UserInputEnvelope,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Publish a pipeline lifecycle event if an ``EventBus`` is available.

    The event is published with ``task_id="pipeline"`` (no Gateway task yet)
    and the correlation ID in the payload for traceability.
    """
    if event_bus is None:
        return
    try:
        # Pipeline observability events are non-critical — log and continue.
        # New-style EventBus (antigona.events.bus) uses typed BaseEvent objects;
        # if the publish signature doesn't match, skip silently.
        await event_bus.publish(
            event_type=event_type,
            task_id="pipeline",
            conversation_id=envelope.chat_id,
            source=envelope.source,
            payload={
                "correlation_id": envelope.correlation_id,
                "message_id": envelope.message_id,
                "user_id": envelope.user_id,
                **(payload or {}),
            },
        )
    except TypeError:
        pass  # new-style EventBus — pipeline events are best-effort only
    except Exception:
        logger.warning("Failed to publish pipeline event %s", event_type, exc_info=True)


# ── Timing context manager ──────────────────────────────────────────────────


@asynccontextmanager
async def timed_stage(
    event_bus: Any | None,
    event_type: str,
    envelope: UserInputEnvelope,
    *,
    payload: dict[str, Any] | None = None,
) -> AsyncGenerator[None, None]:
    """Context manager that logs, emits an EventBus event, and measures duration.

    Usage::

        async with timed_stage(event_bus, PIPELINE_EVENT_CONTEXT_RESOLVED, envelope):
            ctx = await context_resolver.resolve(...)
    """
    start = time.monotonic()
    log_stage(event_type, envelope)
    try:
        yield
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        log_stage(event_type, envelope, duration_ms=duration_ms)
        await publish_pipeline_event(
            event_bus,
            event_type,
            envelope,
            payload={**(payload or {}), "duration_ms": round(duration_ms, 3)},
        )


# ── Log pipeline result ─────────────────────────────────────────────────────


def log_result(result: ProcessingResult) -> None:
    """Log the final pipeline result."""
    if result.success:
        logger.info(
            "Pipeline[COMPLETED] corr=%s task=%s dur=%.1fms",
            result.correlation_id[:12],
            result.task_id[:12] if result.task_id else "-",
            result.duration_ms,
        )
    else:
        logger.warning(
            "Pipeline[FAILED] corr=%s error=%s dur=%.1fms",
            result.correlation_id[:12],
            result.error,
            result.duration_ms,
        )
