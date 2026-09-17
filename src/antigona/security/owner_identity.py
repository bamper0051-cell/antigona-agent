"""OwnerIdentity — проверка личности владельца Antigona.

Владелец задаётся через ANTIGONA_OWNER_ID (Telegram user ID).
Если не задан — доступ владельца считается не сконфигурированным (fail-closed).
Username НЕ используется как идентификатор (§13 мастер-промпта).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class OwnerIdentity:
    """Проверка личности владельца Antigona.

    Владелец задаётся через ANTIGONA_OWNER_ID (Telegram user ID).
    Username НЕ используется как идентификатор.

    Пример::

        identity = OwnerIdentity()
        if identity.is_owner(message.from_user.id):
            await message.answer("Привет, хозяин!")
    """

    def __init__(self, allowed_user_id: int | None = None) -> None:
        """Инициализация проверки владельца.

        Args:
            allowed_user_id: Принудительный ID владельца.
                            Если None — читает из env ANTIGONA_OWNER_ID.
        """
        self._allowed_user_id: int | None = allowed_user_id

        if self._allowed_user_id is None:
            # ANTIGONA_OWNER_ID is canonical; ANTIGONA_TELEGRAM_OWNER_ID is a
            # deprecated alias kept so OwnerOverrideManager (which historically
            # read only that name) resolves the same owner through this one
            # authority (campaign finding CP-6).
            raw = os.environ.get("ANTIGONA_OWNER_ID", "") or os.environ.get(
                "ANTIGONA_TELEGRAM_OWNER_ID", ""
            )
            if raw and raw.strip().lstrip("-").isdigit():
                self._allowed_user_id = int(raw.strip())
                logger.info(
                    "OwnerIdentity: владелец установлен (user_id=%d)",
                    self._allowed_user_id,
                )
            if self._allowed_user_id is None:
                logger.warning(
                    "OwnerIdentity: ANTIGONA_OWNER_ID не задан — доступ владельца закрыт (fail-closed)"
                )

    # ── Проверки ───────────────────────────────────────────────────────────

    def is_owner(self, user_id: int) -> bool:
        """Проверить, является ли пользователь владельцем.

        Args:
            user_id: Telegram user ID для проверки.

        Returns:
            True если пользователь — владелец.
        """
        if self._allowed_user_id is None:
            return False
        return user_id == self._allowed_user_id

    def require_owner(self, user_id: int) -> None:
        """Проверить и вызвать исключение, если пользователь — не владелец.

        Args:
            user_id: Telegram user ID для проверки.

        Raises:
            PermissionError: Если пользователь не является владельцем.
        """
        if self._allowed_user_id is None:
            raise PermissionError(
                "Доступ запрещён: ANTIGONA_OWNER_ID не задан, владелец не сконфигурирован."
            )
        if not self.is_owner(user_id):
            raise PermissionError(
                f"Доступ запрещён: пользователь {user_id} не является владельцем. "
                f"Ожидается user_id={self._allowed_user_id}."
            )

    # ── Свойства ───────────────────────────────────────────────────────────

    @property
    def owner_user_id(self) -> int | None:
        """ID владельца (Telegram user ID) или None если не задан."""
        return self._allowed_user_id

    @property
    def is_configured(self) -> bool:
        """True если владелец явно задан через ANTIGONA_OWNER_ID."""
        return self._allowed_user_id is not None

    # ── Chat ID ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_chat_id() -> int | None:
        """Получить ANTIGONA_TELEGRAM_CHAT_ID из окружения.

        Это ID чата/группы для отправки уведомлений и статусных сообщений.
        Может отличаться от owner_user_id (чат владельца).

        Returns:
            ID чата или None если не задан.
        """
        raw = os.environ.get("ANTIGONA_TELEGRAM_CHAT_ID", "")
        if raw and raw.strip().lstrip("-").isdigit():
            return int(raw.strip())
        return None

    def __repr__(self) -> str:
        status = (
            f"owner_id={self._allowed_user_id}"
            if self._allowed_user_id is not None
            else "owner_id=None (fail-closed)"
        )
        return f"<OwnerIdentity {status}>"
