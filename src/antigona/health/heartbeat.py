"""Service heartbeat + readiness for Antigona's non-HTTP services.

Gateway and Verifier expose HTTP health/readiness endpoints. Worker,
Delivery (and any long-running non-HTTP service) do not, so they write a
periodic heartbeat file to ``<project_root>/.health/<service>.json``. The
Gateway's aggregate ``/status`` endpoint reads these heartbeats (freshness
within a window => "up") and probes the HTTP services directly.

Milestone 0 / P0 Security & Runtime Hygiene — scope 4 (health/readiness).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from antigona.core import paths

logger = logging.getLogger(__name__)

# A service whose heartbeat is older than this is considered DOWN.
FRESHNESS_WINDOW_SECONDS = float(
    os.getenv("ANTIGONA_HEALTH_FRESHNESS_SECONDS", "15")
)


def _health_dir() -> Path:
    """Governed heartbeat directory: ``<ANTIGONA_STATE_ROOT>/health`` in a
    hardened deployment, ``<project_root>/.health`` for a dev/test checkout."""
    d = paths.health_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


class HeartbeatReporter:
    """Daemon thread that refreshes a service's heartbeat file every interval."""

    def __init__(
        self,
        service: str,
        interval_seconds: float = 5.0,
        window_seconds: float = FRESHNESS_WINDOW_SECONDS,
    ) -> None:
        self.service = service
        self.interval = interval_seconds
        self.window = window_seconds
        self._stop = threading.Event()
        self.last_progress = time.time()

    def stamp_progress(self) -> None:
        self.last_progress = time.time()

    def touch(self) -> None:
        self.last_progress = time.time()  # liveness: a beat means the service is alive
        payload = {
            "service": self.service,
            "pid": os.getpid(),
            "ts": time.time(),
            "status": "up",
            "last_progress": self.last_progress,
        }
        tmp = _health_dir() / f"{self.service}.json.tmp"
        final = _health_dir() / f"{self.service}.json"
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(final)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("heartbeat write failed for %s: %s", self.service, exc)

    def start(self) -> HeartbeatReporter:
        self.touch()  # immediate first beat so /status sees us right away
        t = threading.Thread(
            target=self._run, name=f"heartbeat-{self.service}", daemon=True
        )
        t.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.touch()

    def stop(self) -> None:
        self._stop.set()


def read_status(
    window_seconds: float = FRESHNESS_WINDOW_SECONDS,
) -> dict[str, dict[str, object]]:
    """Read all heartbeat files; return per-service liveness.

    Returns ``{service: {"up": bool, "last_seen": float|None, "pid": int|None}}``.
    A heartbeat file missing or older than *window_seconds* => down.
    """
    now = time.time()
    result: dict[str, dict[str, object]] = {}
    d = _health_dir()
    for f in sorted(d.glob("*.json")):
        service = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ts = float(data.get("ts", 0))
            pid = data.get("pid")
            last_progress = float(data.get("last_progress", ts))
            up = (now - ts) <= window_seconds and (now - last_progress) <= window_seconds
            if up and pid:
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    up = False
            result[service] = {
                "up": up,
                "last_seen": ts,
                "pid": pid,
            }
        except (OSError, ValueError, TypeError) as exc:  # pragma: no cover
            logger.warning("unreadable heartbeat %s: %s", f, exc)
            result[service] = {"up": False, "last_seen": None, "pid": None}
    return result
