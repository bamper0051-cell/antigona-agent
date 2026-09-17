from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

_REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?ix)(?:^|[_\-.])(?:api[_\-.]?key|access[_\-.]?token|refresh[_\-.]?token|auth(?:orization)?|"
    r"bearer|password|passwd|pwd|secret|credential|client[_\-.]?secret)(?:$|[_\-.])"
)
_BEARER = re.compile(
    r"(?i)\bBearer\s+(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\]\[{}()\"']+)"
)
_INLINE_SECRET = re.compile(
    r"(?ix)\b(api[_\-.]?key|access[_\-.]?token|refresh[_\-.]?token|token|password|passwd|pwd|"
    r"secret|client[_\-.]?secret|authorization)\b(\s*[=:]\s*)(?:Bearer\s+)?"
    r"([^\s,;\]\[{}\"']+)"
)
# Consume through the last @ in the authority. This safely removes the whole
# userinfo even for percent-encoding or adversarial raw @ in a password.
_URL_USERINFO = re.compile(r"(?i)(https?://)([^\s/?#]*@)")


@dataclass(frozen=True)
class EventEnvelope:
    """Typed flow-event envelope; nullable identifiers are explicit exceptions."""

    service: str
    correlation_id: str
    task_id: str | None
    session_id: str | None
    step_id: str | None
    status: str


def _is_secret_key(key: object) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return bool(_SECRET_KEY.search(f"_{normalized}_"))


def _redact_string(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _INLINE_SECRET.sub(r"\1\2[REDACTED]", value)


def redact(value: object) -> object:
    """Recursively remove credentials from structured and free-form values."""
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def event(name: str, **fields: Any) -> None:
    """Emit one redacted JSON event following the mandatory observability envelope."""
    if not fields.get("service"):
        raise ValueError("observability event requires service")
    if "correlation_id" not in fields:
        raise ValueError("observability event requires correlation_id")
    if fields["service"] in {"gateway", "worker", "verifier"}:
        missing = {"task_id", "session_id", "step_id", "status"} - fields.keys()
        if missing:
            raise ValueError("flow observability event requires " + ", ".join(sorted(missing)))
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payload = {"timestamp": timestamp, "event": name, **fields}
    logging.getLogger("antigona").info(
        json.dumps(redact(payload), sort_keys=True, default=str, separators=(",", ":"))
    )


def flow_event(name: str, envelope: EventEnvelope, **fields: Any) -> None:
    """Emit a typed Gateway/Worker/Verifier event with the complete envelope."""
    event(name, **asdict(envelope), **fields)


@contextmanager
def timed(name: str, **fields: Any) -> Iterator[None]:
    start = time.monotonic()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        event(
            name,
            duration_ms=round((time.monotonic() - start) * 1000, 3),
            status=status,
            **fields,
        )
