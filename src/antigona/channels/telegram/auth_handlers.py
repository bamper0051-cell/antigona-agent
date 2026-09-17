"""Auth command handlers for Telegram bot — /unlock, /lock, /auth_status.

Minimal wiring: called from bot.py command dispatchers.
Uses pin_gate for PIN verification and session management.
"""
from __future__ import annotations

import logging

from aiogram.types import Message

from antigona.security.owner_identity import OwnerIdentity
from antigona.tools.pin_gate import (
    RiskClass,
    attempt_unlock,
    check_unlock_possible,
    elevate_session,
    get_session_info,
    is_pin_configured,
    lock_session,
)

logger = logging.getLogger(__name__)

_OWNER_DENIED_TEXT = (
    "🚫 Доступ запрещён: Telegram ID не принадлежит владельцу.\n"
    "👤 Владелец: не подтверждён\n"
    "🔒 /unlock <PIN> и /confirm <код подтверждения> недоступны."
)

_ACTION_CATEGORY_NAMES = {
    RiskClass.SAFE: "🔵 безопасное",
    RiskClass.SENSITIVE: "🟡 чувствительное",
    RiskClass.CRITICAL: "🔴 критическое",
}


async def cmd_unlock(message: Message) -> str | None:
    """Handle /unlock: verify PIN and elevate session.

    Returns None if already handled (message.answer called internally),
    or a status string for the caller.
    """
    chat_id = message.chat.id
    user_id = getattr(message.from_user, "id", 0)
    identity = OwnerIdentity()

    if not identity.is_owner(user_id):
        await message.answer(_OWNER_DENIED_TEXT)
        return None

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    attempt = parts[1].strip() if len(parts) > 1 else ""

    if not is_pin_configured():
        await message.answer(
            "🔓 PIN не настроен.\n"
            "Установите ANTIGONA_PIN в окружении сервера."
        )
        return None

    if not attempt:
        allowed, msg = check_unlock_possible(chat_id)
        if not allowed:
            await message.answer(f"🔒 {msg}")
            return None
        await message.answer("🔒 Использование: /unlock <PIN-код>")
        return None

    success, msg = attempt_unlock(chat_id, attempt)

    try:
        await message.delete()
    except Exception:
        logger.debug("Failed to delete PIN message in chat_id=%d", chat_id, exc_info=True)

    if not success:
        await message.answer(f"❌ {msg}")
        return None

    # Success — create elevated session
    session = elevate_session(chat_id, user_id)
    await message.answer(
        f"✅ Режим владельца активирован на {session.ttl_seconds // 60} минут."
    )
    return "UNLOCKED"


async def cmd_lock(message: Message) -> str | None:
    """Handle /lock: immediately revoke elevated session."""
    chat_id = message.chat.id
    lock_session(chat_id)
    await message.answer("🔒 Режим владельца отключён.")
    return "LOCKED"


async def cmd_auth_status(message: Message) -> str | None:
    """Handle /auth_status: show current authentication state."""
    chat_id = message.chat.id
    user_id = getattr(message.from_user, "id", 0)
    identity = OwnerIdentity()

    if not identity.is_owner(user_id):
        await message.answer(_OWNER_DENIED_TEXT)
        return None

    info = get_session_info(chat_id)
    if info:
        remaining = info["remaining_seconds"]
        mins = remaining // 60
        secs = remaining % 60
        await message.answer(
            f"👤 Владелец: подтверждён\n"
            f"🔑 Режим владельца: активен\n"
            f"⏱ Осталось: {mins} мин {secs} сек"
        )
    else:
        await message.answer(
            "👤 Владелец: подтверждён\n"
            "🔑 Режим владельца: не активен\n"
            "💡 Используйте /unlock для активации."
        )
    return None


async def cmd_confirm(message: Message) -> str | None:
    """Handle /confirm: confirm a pending action.

    A-4: both confirmation surfaces now end in the SAME canonical mechanism —
    an ApprovalGrantStore one-shot grant. First the pin_gate confirmation
    (per-chat CRITICAL actions), then the PolicyEngine 2-step confirmation
    (``/confirm <token>``), whose bridge mints and consumes the grant. Neither
    resolving => rejected (fail-closed).
    """
    from antigona.policy.engine import confirm_pending_globally
    from antigona.tools.pin_gate import confirm_action_with_grant

    chat_id = message.chat.id
    user_id = getattr(message.from_user, "id", 0)
    identity = OwnerIdentity()

    if not identity.is_owner(user_id):
        await message.answer(_OWNER_DENIED_TEXT)
        return None

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""

    if not token:
        await message.answer("🔒 Использование: /confirm <токен_подтверждения>")
        return None

    granted = confirm_action_with_grant(chat_id, user_id, token)
    if granted is not None:
        await message.answer("✅ Действие подтверждено (выдан одноразовый grant).")
        return "CONFIRMED"

    outcome = await confirm_pending_globally("telegram", user_id, f"/confirm {token}")
    if outcome is not None:
        if outcome.get("allowed"):
            await message.answer("✅ Действие подтверждено (одноразовый grant использован).")
            return "CONFIRMED"
        reason = str(outcome.get("reason") or "подтверждение отклонено")
        await message.answer(f"🚫 {reason}")
        return None

    await message.answer("❌ Неверный или истёкший код подтверждения.")
    return None
