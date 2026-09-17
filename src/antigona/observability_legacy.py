"""Legacy-path reachability telemetry — Control-Plane Sanitation campaign, Wave 0.

STRICTLY ADDITIVE AND BEHAVIOUR-NEUTRAL. ``record()`` bumps an in-process counter
and, only when ``ANTIGONA_LEGACY_TELEMETRY`` is set, appends one JSONL line to
``<runtime>/legacy_reachability.jsonl``. It NEVER raises into its caller and NEVER
changes control flow — every code path is wrapped. It exists so the strangler
waves can prove, with runtime evidence, that a "legacy" or "dormant" path is
actually unused before it is wrapped or deleted (§22 removal gate).

Instrumented paths (Wave 0):
  pin_gate.elevate_session / pin_gate.mark_verified / pin_gate.attempt_unlock
  action_executor.execute
  kernel.executor.execute_run / kernel.dispatcher.run_forever
  orchestration.engine._process_goal
  cli_ui.chat.local_shell_exec        (CP-9 — CLI in-process tool execution)
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter

_LOCK = threading.Lock()
_HITS: Counter[str] = Counter()
_FIRST_SEEN: dict[str, float] = {}
_ENV_FLAG = "ANTIGONA_LEGACY_TELEMETRY"


def record(path: str) -> None:
    """Note that ``path`` (a legacy/dormant code path) was reached. Never raises."""
    try:
        now = time.time()
        with _LOCK:
            _HITS[path] += 1
            _FIRST_SEEN.setdefault(path, now)
        if os.environ.get(_ENV_FLAG):
            _append_jsonl(path, now)
    except Exception:  # telemetry must never affect the instrumented path
        pass


def _append_jsonl(path: str, ts: float) -> None:
    try:
        from antigona.core import paths as _paths

        target = _paths.legacy_reachability_log()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "path": path}) + "\n")
    except Exception:
        pass


def snapshot() -> dict[str, int]:
    """Current hit counts per instrumented path (test / inspection helper)."""
    with _LOCK:
        return dict(_HITS)


def first_seen() -> dict[str, float]:
    with _LOCK:
        return dict(_FIRST_SEEN)


def reset() -> None:
    """Clear counters — tests only."""
    with _LOCK:
        _HITS.clear()
        _FIRST_SEEN.clear()
