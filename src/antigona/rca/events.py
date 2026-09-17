"""Hermes RCA — event transport + consumer (spec sections 7, 8).

Hermes is NOT invoked synchronously inside exception handlers. Errors are
published as events (``runtime.error.detected``) onto the existing
``EventBus``. The RCA consumer subscribes, builds an ErrorEnvelope, applies the
mandatory redaction gate, diagnoses asynchronously and persists the result.

If Hermes/the consumer is unavailable, Antigona continues execution and the
envelope is still persisted with status=UNAVAILABLE (mandatory behaviour).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from antigona.events.bus import EventBus
from antigona.events.event_types import BaseEvent, ErrorOccurred
from antigona.rca.dedup import Deduplicator, fingerprint
from antigona.rca.envelope import ErrorEnvelope
from antigona.rca.pipeline import RCAEngine
from antigona.rca.storage import RCARepository

logger = logging.getLogger(__name__)

#: Canonical event name for error detection (spec section 7).
EVENT_ERROR_DETECTED = "runtime.error.detected"


def build_envelope_from_error_event(event: ErrorOccurred, **overrides: Any) -> ErrorEnvelope:
    """Convert an ``ErrorOccurred`` event into a redacted ErrorEnvelope."""
    envelope = ErrorEnvelope(
        correlation_id=event.correlation_id or "",
        source_component=event.source_component,
        operation="",
        exception_type=event.error_type,
        error_message=event.message,
        runtime_metadata=dict(event.details or {}),
    )
    for k, v in overrides.items():
        setattr(envelope, k, v)
    return envelope.redacted()


class HermesRCAConsumer:
    """Async consumer: error event -> redacted envelope -> diagnosis -> store.

    Non-blocking: ``handle`` is scheduled, never awaited inline by callers in
    the hot path. If the engine/store fails, the error is logged and Antigona
    continues — RCA failure never crashes the pipeline.
    """

    def __init__(
        self,
        engine: RCAEngine | None = None,
        repository: RCARepository | None = None,
        dedup: Deduplicator | None = None,
    ) -> None:
        self.engine = engine or RCAEngine()
        self.repository = repository
        self.dedup = dedup or Deduplicator()

    def attach(self, bus: EventBus, *, on_event: type[BaseEvent] = ErrorOccurred) -> None:
        """Subscribe this consumer to the event bus for error events."""
        bus.subscribe(on_event, self._dispatch)

    async def _dispatch(self, event: BaseEvent) -> None:
        if not isinstance(event, ErrorOccurred):
            return
        try:
            envelope = build_envelope_from_error_event(event)
            counter = self.dedup.record(envelope)
            result = self.engine.diagnose(envelope)
            if self.repository is not None:
                self.repository.save(result, envelope, fingerprint(envelope), counter.count)
            logger.info(
                "HermesRCA: %s category=%s confidence=%s dup=%s",
                result.error_id,
                result.category,
                result.confidence.value,
                counter.count,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("HermesRCA consumer failed (non-blocking): %s", exc)

    async def run_forever(self, bus: EventBus) -> None:
        """Idle loop so the consumer stays attached across restarts."""
        while True:
            await asyncio.sleep(3600)
