"""Server-side session store for Gateway auth.

In-memory session store with 24-hour TTL.
Sessions are created after successful OTP verification and used
to authenticate subsequent dashboard API calls.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ── In-memory session store ──────────────────────────────────────────────────

_sessions: dict[str, dict[str, Any]] = {}
TTL_SECONDS = 86400  # 24 hours


async def create_session(telegram_id: int) -> str:
    """Create a new session for the given telegram_id.

    Args:
        telegram_id: Telegram user ID of the authenticated owner.

    Returns:
        Session token (hex string).
    """
    token = uuid.uuid4().hex
    _sessions[token] = {
        "telegram_id": telegram_id,
        "created_at": time.time(),
    }
    logger.debug("Session created for telegram_id=%d (token=%s…)", telegram_id, token[:8])
    return token


async def get_session(token: str) -> dict[str, Any] | None:
    """Retrieve session data by token.

    Returns None if token is missing or expired (>24h).

    Args:
        token: Session token to look up.

    Returns:
        Session dict with telegram_id and created_at, or None.
    """
    data = _sessions.get(token)
    if data is None:
        return None
    if time.time() - data["created_at"] >= TTL_SECONDS:
        _sessions.pop(token, None)
        logger.debug("Session %s… expired and removed", token[:8])
        return None
    return data


def cleanup_expired_sessions() -> int:
    """Remove all expired sessions from the store.

    Returns:
        Number of removed sessions.
    """
    now = time.time()
    expired = [t for t, d in _sessions.items() if now - d["created_at"] >= TTL_SECONDS]
    for t in expired:
        _sessions.pop(t, None)
    if expired:
        logger.debug("Cleaned up %d expired sessions", len(expired))
    return len(expired)
