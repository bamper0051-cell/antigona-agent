"""In-memory event bus with typed pub/sub, correlation-id propagation,
and cancellation support.

Usage::

    bus = EventBus()
    bus.subscribe(MessageReceived, my_handler)
    await bus.publish(MessageReceived(correlation_id="abc", text="hello"))
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from antigona.events.event_types import (
    BaseEvent,
    Cancelled,
    CancelRequested,
    event_type_from_name,
)

logger = logging.getLogger(__name__)

# Type alias: an async handler that takes an event
EventHandler = Callable[[BaseEvent], Awaitable[None]]


class EventBus:
    """In-memory pub/sub event bus.

    Features:
    - Subscribe by event class (exact type match, no inheritance dispatch)
    - Async publish — all handlers run concurrently via ``asyncio.gather``
    - Handler errors are logged and do not crash the bus
    - Global and per-task cancellation support
    - ``correlation_id`` is automatically propagated from the event to log records
    """

    def __init__(self) -> None:
        self._subscribers: dict[type[BaseEvent], list[EventHandler]] = {}
        self._any_subscribers: list[EventHandler] = []
        # task_id -> set of asyncio.Event for cancellation
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._cancelled_tasks: set[str] = set()

    # ── Subscription ─────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: type[BaseEvent],
        handler: EventHandler,
    ) -> Callable[[], None]:
        """Register *handler* for *event_type*.

        Returns a callable that unsubscribes the handler when invoked.
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

        def _unsubscribe() -> None:
            handlers = self._subscribers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    def subscribe_any(self, handler: EventHandler) -> Callable[[], None]:
        """Register a wildcard handler called for **every** event.

        Returns an unsubscribe callable.
        """
        self._any_subscribers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._any_subscribers:
                self._any_subscribers.remove(handler)

        return _unsubscribe

    def unsubscribe(self, event_type: type[BaseEvent], handler: EventHandler) -> None:
        """Remove a specific handler from an event type (no-op if absent)."""
        handlers = self._subscribers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    # ── Publishing ────────────────────────────────────────────────────────

    async def publish(self, event: BaseEvent) -> None:
        """Publish *event* to all matching subscribers.

        Sets ``event.timestamp`` if still 0.0.
        All handlers run concurrently. Errors are logged individually.
        """
        if event.timestamp == 0.0:
            event.timestamp = time.monotonic()

        cid = event.correlation_id or "(none)"
        logger.debug("EventBus: publish %s [correlation_id=%s]", type(event).__name__, cid)

        tasks: list[Awaitable[None]] = []

        # Type-specific handlers
        handlers = self._subscribers.get(type(event), [])
        for h in handlers:
            tasks.append(self._safe_invoke(h, event))

        # Wildcard handlers
        for h in self._any_subscribers:
            tasks.append(self._safe_invoke(h, event))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for _i, res in enumerate(results):
                if isinstance(res, BaseException):
                    logger.error(
                        "EventBus: handler error for %s [%s]: %s",
                        type(event).__name__,
                        cid,
                        res,
                    )

    async def _safe_invoke(self, handler: EventHandler, event: BaseEvent) -> None:
        """Invoke a single handler, catching and logging exceptions."""
        try:
            await handler(event)
        except Exception as exc:
            logger.exception(
                "EventBus: handler %s raised %s for %s [correlation_id=%s]",
                getattr(handler, "__name__", str(handler)),
                type(exc).__name__,
                type(event).__name__,
                event.correlation_id or "(none)",
            )

    # ── Cancellation ──────────────────────────────────────────────────────

    async def request_cancel(self, task_id: str, reason: str = "",
                             correlation_id: str = "") -> None:
        """Request cancellation of a task.

        Sets the cancel event for the task and publishes ``CancelRequested``.
        """
        if task_id in self._cancelled_tasks:
            return  # already cancelled

        # Signal any in-flight work via asyncio.Event
        if task_id in self._cancel_events:
            self._cancel_events[task_id].set()

        ev = CancelRequested(
            correlation_id=correlation_id,
            task_id=task_id,
            reason=reason,
        )
        await self.publish(ev)

    async def confirm_cancelled(self, task_id: str, reason: str = "",
                                correlation_id: str = "") -> None:
        """Mark a task as cancelled and publish ``Cancelled``."""
        self._cancelled_tasks.add(task_id)
        if task_id in self._cancel_events:
            self._cancel_events.pop(task_id, None)

        ev = Cancelled(
            correlation_id=correlation_id,
            task_id=task_id,
            reason=reason,
        )
        await self.publish(ev)

    def register_cancel_event(self, task_id: str) -> asyncio.Event:
        """Get or create an ``asyncio.Event`` that gets set on cancellation.

        Long-running work should ``await event.wait()`` at safe checkpoints.
        """
        if task_id not in self._cancel_events:
            self._cancel_events[task_id] = asyncio.Event()
        return self._cancel_events[task_id]

    def is_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled (no side effects)."""
        return task_id in self._cancelled_tasks

    def cancel_event(self, task_id: str) -> asyncio.Event | None:
        """Return the cancel ``asyncio.Event`` for *task_id*, or ``None``."""
        return self._cancel_events.get(task_id)

    # ── Serialization helpers ─────────────────────────────────────────────

    @staticmethod
    def event_to_dict(event: BaseEvent) -> dict[str, Any]:
        """Convert any ``BaseEvent`` subclass to a plain dict.

        Includes a ``_type`` key with the class name for deserialisation.
        """
        data: dict[str, Any] = {"_type": type(event).__name__}
        for field_name in event.__dataclass_fields__:
            data[field_name] = getattr(event, field_name)
        return data

    @staticmethod
    def event_from_dict(data: dict[str, Any]) -> BaseEvent | None:
        """Reconstruct a ``BaseEvent`` from a dict previously created by
        :meth:`event_to_dict`.

        Returns ``None`` if the type name is unknown.
        """
        type_name = data.pop("_type", "")
        cls = event_type_from_name(type_name)
        if cls is None:
            return None
        # Filter to only valid fields
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)
