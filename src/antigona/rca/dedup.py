"""Hermes RCA — error deduplication by fingerprint (spec section 14).

Repeated identical errors are aggregated. Fingerprint key:
exception_type | source_component | normalized_message | top_stack_frames | tool/provider
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from antigona.rca.envelope import ErrorEnvelope


def _normalize_message(message: str) -> str:
    """Normalize a message by dropping volatile substrings (ids, numbers, keys)."""
    text = re.sub(r"0x[0-9a-fA-F]{6,}", "<hex>", message)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text)
    text = re.sub(r"\d+", "<n>", text)
    return " ".join(text.lower().split())


def fingerprint(envelope: ErrorEnvelope) -> str:
    """Stable SHA-256 fingerprint for an envelope (aggregation key)."""
    top_frames = "\n".join(envelope.stack_trace.splitlines()[:5]) if envelope.stack_trace else ""
    material = "|".join(
        [
            envelope.exception_type,
            envelope.source_component,
            _normalize_message(envelope.error_message),
            top_frames,
            envelope.tool_name or "",
            envelope.provider or "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class DedupCounter:
    """Tracks how many identical errors share a fingerprint."""

    fingerprint: str
    first_seen: str
    count: int = 1
    last_count_updated: str = ""

    def bump(self, envelope: ErrorEnvelope) -> None:
        self.count += 1
        self.last_count_updated = envelope.timestamp


class Deduplicator:
    """In-memory aggregation map fingerprint -> count (spec section 14)."""

    def __init__(self, window_seconds: float = 180.0) -> None:
        self._window = window_seconds
        self._buckets: dict[str, list[DedupCounter]] = {}

    def record(self, envelope: ErrorEnvelope) -> DedupCounter:
        fp = fingerprint(envelope)
        bucket = self._buckets.setdefault(fp, [])
        # prune entries older than the aggregation window
        now = _epoch(envelope.timestamp)
        counters = [c for c in bucket if now - _epoch(c.first_seen) <= self._window]
        self._buckets[fp] = counters
        if counters:
            counters[0].bump(envelope)
            return counters[0]
        counter = DedupCounter(fp, envelope.timestamp)
        counter.last_count_updated = envelope.timestamp
        self._buckets[fp].append(counter)
        return counter

    def count(self, envelope: ErrorEnvelope) -> int:
        fp = fingerprint(envelope)
        bucket = self._buckets.get(fp, [])
        return bucket[0].count if bucket else 1


def _epoch(iso: str) -> float:
    """Parse an ISO-8601 UTC timestamp into epoch seconds (robust)."""
    try:
        dt = iso.replace("Z", "+00:00")
        from datetime import datetime
        return datetime.fromisoformat(dt).timestamp()
    except Exception:
        return 0.0
