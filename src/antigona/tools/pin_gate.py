"""PIN gate for Antigona Telegram owner authentication.

Three-tier security model:
  SAFE     — read-only, owner ID only
  SENSITIVE — owner ID + elevated session (PIN)
  CRITICAL  — owner ID + elevated session + per-command confirmation

PIN is stored ONLY in env (ANTIGONA_PIN). Never in code, DB, or logs.
SHA-256 hashed comparison. No plaintext PIN in memory longer than needed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from antigona.security.elevation import ElevationAuthority

logger = logging.getLogger(__name__)

# ── Risk classes ────────────────────────────────────────────────────────────


class RiskClass(StrEnum):
    SAFE = "SAFE"
    SENSITIVE = "SENSITIVE"
    CRITICAL = "CRITICAL"


# Actions that modify state or execute code
_SENSITIVE_ACTIONS = frozenset({
    "WRITE_FILE", "RUN_SHELL", "RUN_CODE", "CONFIGURE_KEY",
    "SEND_FILE", "SEARCH_FILES", "INSTALL_PACKAGE", "GIT_MUTATION",
})

# Destructive / irreversible actions  
_CRITICAL_ACTIONS = frozenset({
    "DELETE_FILE", "DROP_DATABASE", "STOP_SERVICE",
    "FIREWALL_CHANGE", "MASS_KILL", "CREDENTIAL_WRITE",
    "SECURITY_DISABLE",
})

# Read-only / safe actions (default if not in any list)
_SAFE_ACTIONS = frozenset({
    "HEALTH_CHECK", "STATUS", "UPTIME", "READ_LOG",
    "READ_FLOW", "LIST_FLOWS", "CPU_MEM_DISK",
})


def classify_action(action_type: str) -> RiskClass:
    """Classify an action into SAFE / SENSITIVE / CRITICAL."""
    upper = action_type.strip().upper()
    if upper in _CRITICAL_ACTIONS:
        return RiskClass.CRITICAL
    if upper in _SENSITIVE_ACTIONS:
        return RiskClass.SENSITIVE
    if upper in _SAFE_ACTIONS:
        return RiskClass.SAFE
    # Unknown actions default to CRITICAL (fail-closed)
    return RiskClass.CRITICAL


# ── Elevation authority (Wave B2 / CP-2) ────────────────────────────────────
# pin_gate no longer keeps its own elevation or brute-force-lockout state. It is
# a thin delegate over the one shared, durable ElevationAuthority — the same
# store OwnerOverrideManager and the CLI PIN gate use. Only PIN *verification*
# (the SHA-256 secret compare) and CRITICAL-confirmation state stay local.

_MAX_ATTEMPTS = 5          # kept for message text + tests; enforced by ElevationAuthority
_LOCKOUT_SECONDS = 900     # kept for docs/tests; enforced by ElevationAuthority
_state_lock = threading.RLock()  # still guards _pending_confirmations below

_elevation: ElevationAuthority | None = None  # lazily the shared store


def _principal(chat_id: int) -> str:
    return f"telegram:chat:{chat_id}"


def _get_elevation() -> ElevationAuthority:
    global _elevation
    if _elevation is None:
        from antigona.security.elevation import owner_elevation_authority

        _elevation = owner_elevation_authority()
    return _elevation


def set_elevation_authority(auth: ElevationAuthority | None) -> None:
    """Inject the ElevationAuthority (tests) or reset to the shared default (None)."""
    global _elevation
    _elevation = auth


def _check_attempts(chat_id: int, *, now: float | None = None) -> bool:
    """True if the chat is not locked out (allowed to try a PIN)."""
    return not _get_elevation().is_locked_out(_principal(chat_id), now=now)


def _record_attempt(chat_id: int, *, now: float | None = None) -> None:
    _get_elevation().record_failure(_principal(chat_id), now=now)


def _reset_attempts(chat_id: int) -> None:
    _get_elevation().record_success(_principal(chat_id))


# ── PIN verification ────────────────────────────────────────────────────────


def _get_pin_hash() -> str | None:
    """Return SHA-256 hex digest of the configured PIN, or None."""
    pin = os.environ.get("ANTIGONA_PIN", "")
    if not pin:
        return None
    return hashlib.sha256(pin.encode()).hexdigest()


def is_pin_configured() -> bool:
    """True if ANTIGONA_PIN is set in environment."""
    return bool(os.environ.get("ANTIGONA_PIN", ""))


def verify_pin(attempt: str) -> bool:
    """Constant-time-ish SHA-256 comparison."""
    pin = os.environ.get("ANTIGONA_PIN", "")
    if not pin or not attempt:
        return False
    expected = hashlib.sha256(pin.encode()).hexdigest()
    actual = hashlib.sha256(attempt.encode()).hexdigest()
    return hmac.compare_digest(expected, actual)


# ── Elevated session management ─────────────────────────────────────────────


@dataclass(frozen=True)
class ElevatedSession:
    """Informational snapshot returned by :func:`elevate_session`.

    The authority for "is this chat elevated / for how long" is
    :class:`~antigona.security.elevation.ElevationAuthority`; this DTO is just the
    values a caller needs right after unlocking.
    """

    session_id: str
    chat_id: int
    user_id: int
    ttl_seconds: int
    remaining_seconds: int


def _get_ttl() -> int:
    """Read TTL from env, default 900s (15 min)."""
    raw = os.environ.get("ANTIGONA_OWNER_ELEVATION_TTL_SECONDS", "900")
    try:
        return max(60, int(raw.strip()))
    except (ValueError, AttributeError):
        return 900


def _derive_session_id(chat_id: int, elevated_at: float) -> str:
    """Stable id for a live elevation, so get_session_info round-trips it."""
    return hashlib.sha1(f"{chat_id}:{elevated_at:.6f}".encode()).hexdigest()


# chat_id -> user_id that unlocked it. NOT elevation authority state (that is
# ElevationAuthority); just the identity the CRITICAL-confirmation flow must use
# instead of trusting the payload. Same process-local scope as
# _pending_confirmations below.
_elevated_user: dict[int, int] = {}


def elevate_session(chat_id: int, user_id: int, *, now: float | None = None) -> ElevatedSession:
    """Create an elevated session for the chat (delegates to ElevationAuthority)."""
    from antigona.observability_legacy import record as _legacy_record
    _legacy_record("pin_gate.elevate_session")
    ttl = _get_ttl()
    auth = _get_elevation()
    auth.elevate(_principal(chat_id), ttl=ttl, now=now)
    with _state_lock:
        _elevated_user[chat_id] = int(user_id)
    meta = auth.session_meta(_principal(chat_id))
    elevated_at = meta[0] if meta else (now if now is not None else 0.0)
    remaining = int(auth.remaining_session(_principal(chat_id), now=now))
    logger.info(
        "Elevated session created for chat_id=%d user_id=%d ttl=%ds", chat_id, user_id, ttl
    )
    return ElevatedSession(
        session_id=_derive_session_id(chat_id, elevated_at),
        chat_id=chat_id,
        user_id=user_id,
        ttl_seconds=ttl,
        remaining_seconds=remaining,
    )


def is_elevated(chat_id: int, *, now: float | None = None) -> bool:
    """True if the chat has an active elevated session."""
    return _get_elevation().is_elevated(_principal(chat_id), now=now)


def get_session_info(chat_id: int, *, now: float | None = None) -> dict[Any, Any] | None:
    """Return info about the current elevated session, or None."""
    auth = _get_elevation()
    if not auth.is_elevated(_principal(chat_id), now=now):
        return None
    meta = auth.session_meta(_principal(chat_id))
    elevated_at = meta[0] if meta else 0.0
    with _state_lock:
        uid = _elevated_user.get(chat_id, 0)
    return {
        "session_id": _derive_session_id(chat_id, elevated_at),
        "chat_id": chat_id,
        "user_id": uid,
        "remaining_seconds": int(auth.remaining_session(_principal(chat_id), now=now)),
        "ttl_seconds": _get_ttl(),
    }


def lock_session(chat_id: int) -> None:
    """Remove the elevated session for this chat (/lock)."""
    _get_elevation().revoke(_principal(chat_id))
    with _state_lock:
        _elevated_user.pop(chat_id, None)
    logger.info("Elevated session locked for chat_id=%d", chat_id)


def requires_pin(action_type: str) -> bool:
    """Backward-compatible helper used by legacy action executor flow."""
    return classify_action(action_type) in {RiskClass.SENSITIVE, RiskClass.CRITICAL}


def mark_verified(
    chat_id: int, ttl_seconds: int | None = None, *, now: float | None = None
) -> None:
    """`/pin` path: mark the chat elevated for TTL seconds.

    Wave B2 (CP-2): this is now the SAME store as :func:`elevate_session` — the
    old separate ``_verified_sessions`` dict is gone, so ``is_verified`` and
    ``is_elevated`` can no longer disagree.
    """
    from antigona.observability_legacy import record as _legacy_record
    _legacy_record("pin_gate.mark_verified")
    ttl = max(1, int(ttl_seconds if ttl_seconds is not None else _get_ttl()))
    _get_elevation().elevate(_principal(chat_id), ttl=ttl, now=now)


def is_verified(chat_id: int, *, now: float | None = None) -> bool:
    """True if the chat is PIN-verified. Same store as :func:`is_elevated` (B2)."""
    return _get_elevation().is_elevated(_principal(chat_id), now=now)


# ── Public API ──────────────────────────────────────────────────────────────


def check_unlock_possible(chat_id: int, *, now: float | None = None) -> tuple[bool, str]:
    """Check if a PIN unlock attempt can proceed.

    Returns:
        (allowed: bool, reason: str)
    """
    if not is_pin_configured():
        return False, "PIN не настроен. Установите ANTIGONA_PIN."
    if not _check_attempts(chat_id, now=now):
        return False, "Слишком много неудачных попыток. Попробуйте через 15 минут."
    return True, ""


def attempt_unlock(chat_id: int, pin_attempt: str, *, now: float | None = None) -> tuple[bool, str]:
    """Try to unlock with a PIN.

    Returns:
        (success: bool, message: str)
    """
    from antigona.observability_legacy import record as _legacy_record
    _legacy_record("pin_gate.attempt_unlock")
    allowed, msg = check_unlock_possible(chat_id, now=now)
    if not allowed:
        return False, msg

    if verify_pin(pin_attempt):
        _reset_attempts(chat_id)
        return True, ""

    locked_now, remaining = _get_elevation().record_failure(_principal(chat_id), now=now)
    if locked_now or remaining <= 0:
        return False, "Неверный PIN. Слишком много попыток — доступ заблокирован на 15 минут."
    return False, f"Неверный PIN. Осталось попыток: {remaining}."


def requires_elevation(action_type: str) -> RiskClass:
    """Return the RiskClass for an action type."""
    return classify_action(action_type)


def reset_all_sessions() -> None:
    """Clear all elevated sessions + lockout + pending confirmations.

    Admin / config-change reset (no production caller today). Not a restart path
    — a restart keeps the durable lockout.
    """
    _get_elevation().clear_all()
    with _state_lock:
        _elevated_user.clear()
        _pending_confirmations.clear()
    logger.info("All elevated sessions and attempt trackers reset")


# ── Confirmation state for CRITICAL actions ────────────────────────────────

_pending_confirmations: dict[int, dict[str, Any]] = {}  # chat_id → {action_data}


def _get_confirm_ttl() -> int:
    """Read confirmation TTL from env, default 60s, minimum 10s."""
    raw = os.environ.get("ANTIGONA_CONFIRM_TTL_SECONDS", "60")
    try:
        return max(10, int(raw.strip()))
    except (TypeError, ValueError, AttributeError):
        return 60


def _canonical_payload_json(payload: dict[Any, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _resolve_confirmation_user_id(chat_id: int, payload: dict[Any, Any]) -> int:
    # P1 security fix (review major): user identity MUST come from the elevated
    # session, never from untrusted payload content. A payload-supplied
    # "user_id" would allow user substitution in the confirmation flow.
    del payload  # intentionally unused — payload must not influence identity
    with _state_lock:
        uid = _elevated_user.get(chat_id)
    if uid is not None and is_elevated(chat_id):
        return uid
    return 0


def set_pending_confirmation(chat_id: int, action_type: str,
                             payload: dict[Any, Any]) -> str:
    """Store a confirmation request and return a confirmation ID."""
    payload_canonical = _canonical_payload_json(payload)
    with _state_lock:
        user_id = _resolve_confirmation_user_id(chat_id, payload)
        digest = hashlib.sha256(
            f"{chat_id}|{user_id}|{action_type}|{payload_canonical}".encode()
        ).hexdigest()
        cid = digest[:16]
        _pending_confirmations[chat_id] = {
            "confirmation_id": cid,
            "action_type": action_type,
            "payload": payload,
            "user_id": user_id,
            "created_at": time.monotonic(),
            "ttl_seconds": _get_confirm_ttl(),
        }
        return cid


def _is_pending_expired(data: dict[str, Any], now: float) -> bool:
    ttl_seconds = data.get("ttl_seconds", 60)
    if not isinstance(ttl_seconds, int):
        ttl_seconds = 60
    created_at = data.get("created_at", 0.0)
    if not isinstance(created_at, (int, float)):
        created_at = 0.0
    return now - float(created_at) > float(ttl_seconds)


def has_pending_confirmation(chat_id: int) -> bool:
    """Check if there's a pending confirmation for this chat."""
    now = time.monotonic()
    with _state_lock:
        data = _pending_confirmations.get(chat_id)
        if data is None:
            return False
        if _is_pending_expired(data, now):
            del _pending_confirmations[chat_id]
            return False
        return True


def get_pending_confirmation(chat_id: int) -> dict[str, Any] | None:
    """Get pending confirmation data, or None."""
    now = time.monotonic()
    with _state_lock:
        data = _pending_confirmations.get(chat_id)
        if data is None:
            return None
        if _is_pending_expired(data, now):
            del _pending_confirmations[chat_id]
            return None
        return data.copy()


def confirm_action(chat_id: int, user_id: int,
                   confirmation_id: str) -> dict[Any, Any] | None:
    """Confirm a pending action. Returns payload if confirmed, None on mismatch."""
    now = time.monotonic()
    with _state_lock:
        data = _pending_confirmations.get(chat_id)
        if data is None:
            return None
        if _is_pending_expired(data, now):
            del _pending_confirmations[chat_id]
            return None
        if data.get("confirmation_id") != confirmation_id:
            return None
        if data.get("user_id") != user_id:
            return None
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None
        del _pending_confirmations[chat_id]
        return payload


def confirm_action_with_grant(
    chat_id: int, user_id: int, confirmation_id: str, grant_store: Any | None = None
) -> tuple[dict[Any, Any], str] | None:
    """Confirm a pending action AND mint the canonical one-shot approval grant.

    A-4: ``/confirm`` used to resolve nothing but an in-memory flag, which
    authorized nothing downstream. The confirmation is now the APPROVAL step of
    the canonical model: it mints an :class:`ApprovalGrantStore` grant bound to
    actor + action + exact payload, which the executing path consumes exactly
    once.

    Returns ``(payload, raw_grant_token)``, or ``None`` when the confirmation
    does not resolve or the grant cannot be minted — fail-closed: no grant, no
    approval.
    """
    pending = get_pending_confirmation(chat_id)
    action_type = str((pending or {}).get("action_type") or "")
    payload = confirm_action(chat_id, user_id, confirmation_id)
    if payload is None or not action_type:
        return None
    try:
        from antigona.security.approval_grant import ApprovalGrantStore

        store = grant_store if grant_store is not None else ApprovalGrantStore()
        token: str = store.issue(
            actor=str(user_id),
            tool_name=action_type,
            args=payload,
            issuer="pin-gate-confirm",
            channel="telegram",
            session_id=str(chat_id),
            reason=action_type,
        )
    except Exception:
        logger.exception("approval grant minting failed for /confirm (fail-closed)")
        return None
    return payload, token


def cancel_confirmation(chat_id: int) -> None:
    """Cancel a pending confirmation."""
    with _state_lock:
        _pending_confirmations.pop(chat_id, None)
