"""MLflow-based observability layer.

Lightweight wrapper around MLflow Tracking API for logging LLM/agent activity
(metrics, params, text artifacts) with a graceful no-op fallback when MLflow is
unavailable or disabled — so nothing hard-fails at import or call time.

Enable by setting ``ANTIGONA_MLFLOW_TRACKING_URI`` (e.g. ``sqlite:///./.mlruns/mlflow.db``)
or ``ANTIGONA_OBSERVABILITY=1``. When unset, calls are no-ops (safe default).

Note: MLflow >= 3.15 requires a database backend (sqlite/postgres); the legacy
file-store backend is rejected unless ``MLFLOW_ALLOW_FILE_STORE=true``. This
module defaults to a SQLite tracking store.

Usage::

    from antigona.core.observability import log_agent_turn

    log_agent_turn(
        session_id="s-1",
        prompt="...",
        reply="...",
        tokens_in=120,
        tokens_out=45,
        model="deepseek-v4-flash",
    )
"""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import Any

__all__ = ["observability_enabled", "log_agent_turn", "start_run", "end_run"]

_DEFAULT_URI = "sqlite:///./.mlruns/mlflow.db"


def observability_enabled() -> bool:
    """True when observability is switched on via env, else False (no-op)."""
    return bool(os.getenv("ANTIGONA_OBSERVABILITY")) or bool(
        os.getenv("ANTIGONA_MLFLOW_TRACKING_URI")
    )


_mlflow = None
_mlflow_error: str | None = None


def _default_uri() -> str:
    uri = os.getenv("ANTIGONA_MLFLOW_TRACKING_URI", _DEFAULT_URI)
    if uri.startswith("sqlite:///"):
        # ensure parent dir exists for the sqlite file
        db_path = pathlib.Path(uri[len("sqlite:///"):])
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return uri


def _load_mlflow() -> Any | None:
    """Lazy-import mlflow; cache result so we only pay once."""
    global _mlflow, _mlflow_error
    if _mlflow is None and _mlflow_error is None:
        try:
            import mlflow as _m

            _m.set_tracking_uri(_default_uri())
            _mlflow = _m
        except Exception as exc:
            _mlflow_error = str(exc)
            _mlflow = None
    return _mlflow or None


def start_run(run_name: str | None = None) -> Any | None:
    """Start an MLflow run if enabled; else no-op sentinel."""
    if not observability_enabled():
        return None
    m = _load_mlflow()
    if m is None:
        return None
    try:
        return m.start_run(run_name=run_name or f"antigona-{uuid.uuid4().hex[:8]}")
    except Exception:
        return None


def end_run() -> None:
    if not observability_enabled():
        return
    m = _load_mlflow()
    if m is None:
        return
    try:
        m.end_run()
    except Exception:
        pass


def log_agent_turn(
    *,
    session_id: str,
    prompt: str,
    reply: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str = "",
    duration_ms: float = 0.0,
    status: str = "ok",
    extra_params: dict[str, Any] | None = None,
) -> bool:
    """Log one agent turn as an MLflow run. Returns True if actually logged."""
    if not observability_enabled():
        return False
    m = _load_mlflow()
    if m is None:
        return False
    try:
        with m.start_run(run_name=f"turn-{uuid.uuid4().hex[:8]}"):
            m.log_params(
                {
                    "session_id": str(session_id),
                    "model": model or "unknown",
                    "status": status,
                    **({} if not extra_params else {k: str(v) for k, v in extra_params.items()}),
                }
            )
            m.log_metrics(
                {
                    "tokens_in": float(tokens_in),
                    "tokens_out": float(tokens_out),
                    "duration_ms": float(duration_ms),
                }
            )
            m.log_text(prompt[:4000], "prompt.txt")
            m.log_text(reply[:4000], "reply.txt")
        return True
    except Exception:
        return False
