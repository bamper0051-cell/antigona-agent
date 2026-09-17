"""Cache-aside cache for task state (P4.3).

The state machine is unchanged: the transition graph, the revision CAS and the
append-only ``state_transitions`` journal all still live in exactly one place —
the SQL transaction. This module only makes the *read* path cheaper.

Three rules keep the cache from ever becoming authoritative:

1. **Write-after-commit only.** :meth:`StateCache.stage` buffers the new state and
   an ``after_commit`` hook publishes it. A rolled-back transaction publishes
   nothing and invalidates instead, so a refused transition can never be cached.
2. **The database wins.** :func:`read_state` treats a miss (or any cache error)
   as "ask SQL", and refreshes the cache from what SQL returned. A TTL bounds
   how long a stale entry can survive even if a writer dies mid-flight.
3. **Failures are invisible.** Every backend error is swallowed into a warning
   event; a broken cache degrades latency, never correctness — and never breaks
   a transition.

``DONE`` remains Verifier-only: the cache is downstream of the graph check, so
there is no cache-shaped path into a state the graph forbids.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TaskFlow
from ..observability import event as log_event

DEFAULT_TTL_SECONDS = 300
KEY_TEMPLATE = "state:{task_id}"

_PENDING = "_antigona_state_cache_pending"
_BOUND = "_antigona_state_cache_bound"


@runtime_checkable
class StateCacheBackend(Protocol):
    """Minimal key/value contract; deliberately smaller than the Redis API."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryStateBackend:
    """TTL-aware in-process backend for tests and single-process deployments."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._values[key]
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._values[key] = (value, self._clock() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class RedisStateBackend:
    """Redis backend with a lazy ``redis`` import and fail-soft semantics."""

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self.url = url
        self._client = client
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _connect(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client

    def _degrade(self, operation: str, exc: Exception) -> None:
        first_time = not self._degraded
        self._degraded = True
        if first_time:
            log_event(
                "state_cache.degraded",
                service="state_cache",
                correlation_id=None,
                status="degraded",
                operation=operation,
                error_type=type(exc).__name__,
                detail="redis unavailable; state reads fall back to SQL",
            )

    def get(self, key: str) -> str | None:
        if self._degraded:
            return None
        try:
            value = self._connect().get(key)
        except Exception as exc:  # cache errors must never surface to callers
            self._degrade("get", exc)
            return None
        return None if value is None else str(value)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if self._degraded:
            return None
        try:
            self._connect().set(key, value, ex=ttl_seconds)
        except Exception as exc:
            self._degrade("set", exc)
        return None

    def delete(self, key: str) -> None:
        if self._degraded:
            return None
        try:
            self._connect().delete(key)
        except Exception as exc:
            self._degrade("delete", exc)
        return None


class StateCache:
    """Cache-aside view of ``task_flows.status``; ``backend=None`` disables it."""

    def __init__(
        self,
        backend: StateCacheBackend | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.backend = backend
        self.ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self.backend is not None

    @staticmethod
    def key(task_id: str) -> str:
        return KEY_TEMPLATE.format(task_id=task_id)

    def get(self, task_id: str) -> str | None:
        if self.backend is None:
            return None
        try:
            return self.backend.get(self.key(task_id))
        except Exception as exc:
            self._warn("get", exc)
            return None

    def put(self, task_id: str, state: str) -> None:
        if self.backend is None:
            return None
        try:
            self.backend.set(self.key(task_id), state, self.ttl_seconds)
        except Exception as exc:
            self._warn("put", exc)
        return None

    def invalidate(self, task_id: str) -> None:
        if self.backend is None:
            return None
        try:
            self.backend.delete(self.key(task_id))
        except Exception as exc:
            self._warn("invalidate", exc)
        return None

    def stage(self, session: Session, task_id: str, state: str) -> None:
        """Record ``state`` for publication *after* the session commits.

        Called from inside the transition's unit of work: if the caller rolls
        back (rejected transition, CAS loss, crash), the staged value is dropped
        and the key invalidated instead of published.
        """
        if self.backend is None:
            return None
        pending: dict[str, str] = session.info.setdefault(_PENDING, {})
        pending[task_id] = state
        self._bind(session)
        return None

    def _bind(self, session: Session) -> None:
        if session.info.get(_BOUND):
            return None
        session.info[_BOUND] = True

        @sqlalchemy_event.listens_for(session, "after_commit")
        def _publish(bound: Session) -> None:
            for task_id, state in bound.info.pop(_PENDING, {}).items():
                self.put(task_id, state)

        @sqlalchemy_event.listens_for(session, "after_soft_rollback")
        def _discard(bound: Session, _previous: object) -> None:
            for task_id in bound.info.pop(_PENDING, {}):
                self.invalidate(task_id)

        return None

    def _warn(self, operation: str, exc: Exception) -> None:
        log_event(
            "state_cache.error",
            service="state_cache",
            correlation_id=None,
            status="degraded",
            operation=operation,
            error_type=type(exc).__name__,
        )


def read_state(session: Session, task_id: str, cache: StateCache | None = None) -> str | None:
    """Read a task's state cache-aside: cache first, then SQL, then refill.

    SQL is the arbiter — a cache miss, a stale entry expiring, or a cache outage
    all converge on the same answer.
    """
    if cache is not None:
        cached = cache.get(task_id)
        if cached is not None:
            return cached
    state = session.scalar(select(TaskFlow.status).where(TaskFlow.id == task_id))
    if state is not None and cache is not None:
        cache.put(task_id, state)
    return state


def build_state_cache(
    redis_url: str | None,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> StateCache:
    """Return a Redis-backed cache, or a disabled one when Redis is not configured."""
    if not redis_url:
        return StateCache(backend=None, ttl_seconds=ttl_seconds)
    return StateCache(backend=RedisStateBackend(redis_url), ttl_seconds=ttl_seconds)
