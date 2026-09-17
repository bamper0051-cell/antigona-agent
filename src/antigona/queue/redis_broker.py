"""Task brokers (P4.3) — a wake-up transport in front of the SQL queue.

**Redis is never the source of truth.** ``queue_jobs`` in SQL, with its lease and
revision CAS (:class:`antigona.queue.DurableQueue`), decides who owns a task. A
broker only carries the signal "there is work now", so a worker can block on
``BLPOP`` instead of polling. Every consequence follows from that:

* a lost Redis message costs latency, never a task — the worker's polling
  fallback still claims the job from SQL;
* a duplicated Redis message is harmless — the SQL CAS lets exactly one worker win;
* Redis being down degrades to the pre-P4.3 behaviour (pure SQL polling) with a
  warning event, never to a stalled or an "open" worker.

``InMemoryBroker`` is the CI/mock implementation: no sockets, no dependencies.
``NullBroker`` is what ``redis_url=None`` yields, making P4.3 a no-op by default.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..observability import event as log_event

_T = TypeVar("_T")

#: Redis key namespace. ``{lane}`` mirrors ``queue_jobs.lane``.
QUEUE_KEY = "antigona:queue:{lane}"
INFLIGHT_KEY = "antigona:inflight:{lane}"

DEFAULT_LANE = "main"


@runtime_checkable
class TaskBroker(Protocol):
    """Wake-up transport contract. All methods are best-effort by design."""

    async def enqueue(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None: ...

    async def dequeue(
        self, *, timeout: float = 1.0, lane: str = DEFAULT_LANE
    ) -> str | None: ...

    async def ack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None: ...

    async def nack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None: ...

    async def close(self) -> None: ...

    def signal(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        """Sync fire-and-forget enqueue, for sync callers (gateway, cron)."""

    def wait(self, timeout: float, *, lane: str = DEFAULT_LANE) -> str | None:
        """Sync blocking wait for a wake-up; returns the signalled task id, if any."""


class NullBroker:
    """No broker configured: the worker keeps its pre-P4.3 SQL polling loop."""

    async def enqueue(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        return None

    async def dequeue(self, *, timeout: float = 1.0, lane: str = DEFAULT_LANE) -> str | None:
        return None

    async def ack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        return None

    async def nack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        return None

    async def close(self) -> None:
        return None

    def signal(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        return None

    def wait(self, timeout: float, *, lane: str = DEFAULT_LANE) -> str | None:
        # Identical to the P0..P4.2 idle sleep, so behaviour is unchanged.
        time.sleep(timeout)
        return None


class InMemoryBroker:
    """In-process broker for tests and CI — never touches the network."""

    def __init__(self) -> None:
        self._lanes: dict[str, deque[str]] = {}
        self._inflight: dict[str, set[str]] = {}
        self.closed = False

    def _lane(self, lane: str) -> deque[str]:
        return self._lanes.setdefault(lane, deque())

    def pending(self, lane: str = DEFAULT_LANE) -> list[str]:
        return list(self._lane(lane))

    def inflight(self, lane: str = DEFAULT_LANE) -> set[str]:
        return set(self._inflight.get(lane, set()))

    async def enqueue(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        self._lane(lane).append(task_id)

    async def dequeue(self, *, timeout: float = 1.0, lane: str = DEFAULT_LANE) -> str | None:
        queue = self._lane(lane)
        if not queue:
            return None
        task_id = queue.popleft()
        self._inflight.setdefault(lane, set()).add(task_id)
        return task_id

    async def ack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        self._inflight.setdefault(lane, set()).discard(task_id)

    async def nack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        self._inflight.setdefault(lane, set()).discard(task_id)
        self._lane(lane).appendleft(task_id)

    async def close(self) -> None:
        self.closed = True

    def signal(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        self._lane(lane).append(task_id)

    def wait(self, timeout: float, *, lane: str = DEFAULT_LANE) -> str | None:
        queue = self._lane(lane)
        return queue.popleft() if queue else None


class RedisTaskBroker:
    """Redis-backed wake-up transport (``RPUSH`` / ``BLPOP``).

    Nothing here is authoritative: ``dequeue`` hands back a *hint*, and the
    caller still has to win ``DurableQueue.claim`` in SQL. Every Redis failure is
    swallowed into a one-shot warning and flips the broker to ``degraded``, after
    which all operations become no-ops and the worker falls back to SQL polling.
    """

    def __init__(
        self,
        url: str,
        *,
        visibility_timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.visibility_timeout = visibility_timeout
        self._client = client
        self._degraded = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def degraded(self) -> bool:
        """True once Redis has failed; the SQL path is then the only path."""
        return self._degraded

    def _connect(self) -> Any:
        """Lazily create the ``redis.asyncio`` client (import happens here only)."""
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.url, decode_responses=True)
        return self._client

    def _degrade(self, operation: str, exc: Exception) -> None:
        first_time = not self._degraded
        self._degraded = True
        if first_time:
            log_event(
                "queue.broker.degraded",
                service="queue",
                correlation_id=None,
                status="degraded",
                operation=operation,
                error_type=type(exc).__name__,
                detail="redis unavailable; falling back to SQL polling",
            )

    async def enqueue(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        if self._degraded:
            return None
        try:
            await self._connect().rpush(QUEUE_KEY.format(lane=lane), task_id)
        except Exception as exc:  # best-effort transport, never fatal
            self._degrade("enqueue", exc)
        return None

    async def dequeue(self, *, timeout: float = 1.0, lane: str = DEFAULT_LANE) -> str | None:
        if self._degraded:
            return None
        try:
            client = self._connect()
            popped = await client.blpop([QUEUE_KEY.format(lane=lane)], timeout=timeout)
            if not popped:
                return None
            task_id = str(popped[1])
            deadline = time.monotonic() + self.visibility_timeout
            await client.hset(INFLIGHT_KEY.format(lane=lane), task_id, str(deadline))
            return task_id
        except Exception as exc:
            self._degrade("dequeue", exc)
            return None

    async def ack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        if self._degraded:
            return None
        try:
            await self._connect().hdel(INFLIGHT_KEY.format(lane=lane), task_id)
        except Exception as exc:
            self._degrade("ack", exc)
        return None

    async def nack(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        if self._degraded:
            return None
        try:
            client = self._connect()
            await client.hdel(INFLIGHT_KEY.format(lane=lane), task_id)
            await client.lpush(QUEUE_KEY.format(lane=lane), task_id)
        except Exception as exc:
            self._degrade("nack", exc)
        return None

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return None
        try:
            await client.aclose()
        except Exception as exc:
            self._degrade("close", exc)
        return None

    def _sync_loop(self) -> asyncio.AbstractEventLoop:
        """A private loop, so the lazily built client stays bound to one loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_sync(self, factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._sync_loop().run_until_complete(factory())
        # Inside a running loop a sync bridge would deadlock: degrade instead.
        self._degrade("sync-bridge", RuntimeError("event loop already running"))
        return None

    def signal(self, task_id: str, *, lane: str = DEFAULT_LANE) -> None:
        if self._degraded:
            return None
        self._run_sync(lambda: self.enqueue(task_id, lane=lane))
        return None

    def wait(self, timeout: float, *, lane: str = DEFAULT_LANE) -> str | None:
        if self._degraded:
            # Keep the idle cadence of the pure-SQL loop instead of spinning.
            time.sleep(timeout)
            return None
        return self._run_sync(lambda: self.dequeue(timeout=timeout, lane=lane))


def build_broker(
    redis_url: str | None,
    *,
    visibility_timeout: float = 30.0,
) -> TaskBroker:
    """Return the broker implied by configuration.

    ``redis_url=None`` yields :class:`NullBroker`, i.e. exactly the pre-P4.3
    behaviour — that is the documented default (ADR-0014).
    """
    if not redis_url:
        return NullBroker()
    return RedisTaskBroker(redis_url, visibility_timeout=visibility_timeout)
