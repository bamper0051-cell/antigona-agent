"""OTPManager — одноразовые коды подтверждения через Telegram (§15).

Архитектура:
  - Генерация криптостойкого 6-значного кода через secrets
  - Код хранится ТОЛЬКО в виде SHA-256 хеша (никогда в plaintext)
  - Отправляется ТОЛЬКО в личный чат владельца через Bot API
  - Никогда не пишется в логи, не попадает в git
  - Challenge привязан к user_id + task_id + action
  - Действует 2 минуты, одноразовое использование
  - После 3 неверных попыток — блокировка
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from antigona.security.risk_classifier import RiskLevel

logger = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────────

def _security_dir() -> Path:
    """Runtime security persistence dir (governed, never the code root)."""
    from antigona.core import paths

    return paths.security_dir()


SECURITY_DIR = _security_dir()
OTP_CODE_LENGTH = 6           # Длина кода
OTP_CODE_LIFETIME = 120       # Секунд жизни кода (2 минуты)
OTP_MAX_ATTEMPTS = 3          # Максимум неверных попыток
OTP_CLEANUP_INTERVAL = 60     # Интервал очистки просроченных (сек)


# ── Модель челленджа ───────────────────────────────────────────────────────


@dataclass
class OTPChallenge:
    """Одноразовый челлендж для подтверждения опасного действия.

    Хранит ТОЛЬКО хеш кода, НИКОГДА не хранит plaintext код.
    """

    challenge_id: str = ""
    user_id: int = 0
    task_id: str = ""
    action_description: str = ""
    action_type: str = ""
    risk_level: str = ""
    salt: str = ""
    code_hash: str = ""  # SHA-256 of the OTP code — never plaintext
    created_at: float = 0.0
    expires_at: float = 0.0
    max_attempts: int = OTP_MAX_ATTEMPTS
    attempt_count: int = 0
    used: bool = False

    def is_expired(self) -> bool:
        """Проверить, истекло ли время жизни челленджа."""
        return time.time() > self.expires_at

    def is_blocked(self) -> bool:
        """Проверить, превышен ли лимит попыток."""
        return self.attempt_count >= self.max_attempts

    def can_attempt(self) -> bool:
        """Можно ли ещё попробовать ввести код?"""
        return not self.used and not self.is_expired() and not self.is_blocked()

    def to_dict(self) -> dict[str, Any]:
        """Сериализация для JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OTPChallenge:
        """Десериализация из JSON."""
        return cls(**data)


# ── OTP Manager ────────────────────────────────────────────────────────────


class OTPManager:
    """Генерация и проверка одноразовых кодов для опасных действий.

    Usage::

        otp = OTPManager()
        challenge = await otp.create_challenge(
            user_id=12345,
            task_id="task_abc",
            action_description="Удаление директории /srv/data",
            action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH,
            bot=bot_instance,
            owner_chat_id=12345,
        )
        # Пользователю отправлен код в Telegram
        is_valid = await otp.verify_code(challenge.challenge_id, "483921")

    Args:
        persist_path: Путь к файлу персистентности (JSON).
                      По умолчанию .security/challenges.json.
        event_publisher: Опциональный callable для публикации SECURITY_EVENT.
                        Вызывается с dict-данными события.
    """

    def __init__(
        self,
        persist_path: str | Path | None = None,
        event_publisher: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if persist_path is None:
            persist_path = SECURITY_DIR / "challenges.json"
        self._persist_path = Path(persist_path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)

        self._event_publisher = event_publisher
        self._challenges: dict[str, OTPChallenge] = {}
        self._lock = asyncio.Lock()
        self._plaintext_codes: dict[str, str] = {}  # ВРЕМЕННОЕ хранение для отправки

        # Загружаем сохранённые челленджи
        self._load()

        logger.info(
            "OTPManager инициализирован (persist=%s, %d активных челленджей)",
            self._persist_path,
            len(self._challenges),
        )

    # ── Создание челленджа ────────────────────────────────────────────────

    async def create_challenge(
        self,
        user_id: int,
        task_id: str,
        action_description: str,
        action_type: str,
        risk_level: RiskLevel,
        bot: Any,
        owner_chat_id: int,
    ) -> OTPChallenge:
        """Создать OTP-челлендж и отправить код владельцу в Telegram.

        Шаги §15:
        1. Генерация криптостойкого 6-значного кода
        2. Хеширование (SHA-256)
        3. Сохранение челленджа с хешем (НЕ plaintext кодом)
        4. Отправка кода в личный чат владельца через bot.sendMessage()
        5. Возврат челленджа

        Args:
            user_id: ID пользователя, инициировавшего действие.
            task_id: ID задачи.
            action_description: Человекочитаемое описание действия.
            action_type: Тип действия (ActionType).
            risk_level: Уровень риска.
            bot: Экземпляр Telegram бота (с методом send_message).
            owner_chat_id: Telegram chat_id владельца для отправки кода.

        Returns:
            OTPChallenge — созданный челлендж.
        """
        # 1. Генерация криптостойкого кода
        code = self._generate_code()

        salt = secrets.token_hex(16)
        
        # 2. Хеширование
        code_hash = self._hash_code(code, salt)

        # 3. Создание челленджа
        now = time.time()
        challenge = OTPChallenge(
            challenge_id=uuid.uuid4().hex,
            user_id=user_id,
            task_id=task_id,
            action_description=action_description,
            action_type=action_type,
            risk_level=risk_level.value,
            salt=salt,
            code_hash=code_hash,
            created_at=now,
            expires_at=now + OTP_CODE_LIFETIME,
            max_attempts=OTP_MAX_ATTEMPTS,
        )

        async with self._lock:
            self._challenges[challenge.challenge_id] = challenge
            # Временное хранение plaintext кода для отправки (удаляется после отправки)
            self._plaintext_codes[challenge.challenge_id] = code
            self._save()

        # 4. Отправка кода владельцу в Telegram
        try:
            await self._send_code_via_bot(
                bot=bot,
                chat_id=owner_chat_id,
                code=code,
                description=action_description,
            )
        except Exception as exc:
            logger.error(
                "OTP: не удалось отправить код владельцу chat_id=%d: %s",
                owner_chat_id,
                exc,
            )
            # Даже если не удалось отправить — челлендж существует
            # (можно повторить отправку позже)
        else:
            # Код доставлен: plaintext больше не нужен (verify работает по
            # хешу), немедленно убираем его из памяти — никогда не оставляем
            # plaintext дольше, чем необходимо.
            async with self._lock:
                self._plaintext_codes.pop(challenge.challenge_id, None)
                self._save()

        # Публикуем событие безопасности
        self._emit_security_event({
            "event": "OTP_CHALLENGE_CREATED",
            "challenge_id": challenge.challenge_id[:8],
            "user_id": user_id,
            "task_id": task_id[:8],
            "action_type": action_type,
            "risk_level": risk_level.value,
            "expires_at": challenge.expires_at,
        })

        logger.info(
            "OTP: челлендж %s создан для user=%d task=%s (action=%s, risk=%s)",
            challenge.challenge_id[:8],
            user_id,
            task_id[:8],
            action_type,
            risk_level.value,
        )
        return challenge

    # ── Проверка кода ─────────────────────────────────────────────────────

    async def verify_code(
        self, challenge_id: str, code_attempt: str
    ) -> bool:
        """Проверить введённый пользователем OTP-код.

        §15 шаги проверки:
        1. Найти челлендж по ID
        2. Проверить срок действия
        3. Проверить, что не использован
        4. Проверить лимит попыток
        5. Захешировать попытку, сравнить с хешем
        6. При совпадении — пометить использованным, опубликовать SECURITY_EVENT
        7. При несовпадении — увеличить счётчик попыток

        Args:
            challenge_id: ID челленджа.
            code_attempt: Введённый пользователем код.

        Returns:
            True если код верен, False в противном случае.

        Note:
            Код попытки НЕ логируется — это нарушение §19.
        """
        async with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                logger.warning("OTP: челлендж %s не найден", challenge_id[:8])
                self._emit_security_event({
                    "event": "OTP_VERIFY_FAILED",
                    "challenge_id": challenge_id[:8],
                    "reason": "CHALLENGE_NOT_FOUND",
                })
                return False

            # 2. Проверка срока действия
            if challenge.is_expired():
                logger.warning("OTP: челлендж %s истёк", challenge_id[:8])
                self._emit_security_event({
                    "event": "OTP_VERIFY_FAILED",
                    "challenge_id": challenge_id[:8],
                    "task_id": challenge.task_id[:8],
                    "reason": "EXPIRED",
                })
                return False

            # 3. Проверка на повторное использование
            if challenge.used:
                logger.warning("OTP: челлендж %s уже использован", challenge_id[:8])
                self._emit_security_event({
                    "event": "OTP_VERIFY_FAILED",
                    "challenge_id": challenge_id[:8],
                    "task_id": challenge.task_id[:8],
                    "reason": "ALREADY_USED",
                })
                return False

            # 4. Проверка лимита попыток
            if challenge.is_blocked():
                logger.warning(
                    "OTP: челлендж %s заблокирован (%d/%d попыток)",
                    challenge_id[:8],
                    challenge.attempt_count,
                    challenge.max_attempts,
                )
                self._emit_security_event({
                    "event": "OTP_BLOCKED",
                    "challenge_id": challenge_id[:8],
                    "task_id": challenge.task_id[:8],
                    "attempt_count": challenge.attempt_count,
                    "max_attempts": challenge.max_attempts,
                })
                return False

            # 5. Хеширование попытки и сравнение
            attempt_hash = self._hash_code(code_attempt, challenge.salt)
            if attempt_hash == challenge.code_hash:
                # УСПЕХ — код верен
                challenge.used = True
                self._save()

                self._emit_security_event({
                    "event": "OTP_VERIFIED",
                    "challenge_id": challenge_id[:8],
                    "task_id": challenge.task_id[:8],
                    "user_id": challenge.user_id,
                    "action_type": challenge.action_type,
                    "risk_level": challenge.risk_level,
                    "action_description": challenge.action_description,
                })

                logger.info(
                    "OTP: код подтверждён для челленджа %s (task=%s, action=%s)",
                    challenge_id[:8],
                    challenge.task_id[:8],
                    challenge.action_type,
                )
                return True
            else:
                # НЕУДАЧА — неверный код
                challenge.attempt_count += 1
                self._save()

                self._emit_security_event({
                    "event": "OTP_VERIFY_FAILED",
                    "challenge_id": challenge_id[:8],
                    "task_id": challenge.task_id[:8],
                    "reason": "WRONG_CODE",
                    "attempt_count": challenge.attempt_count,
                    "max_attempts": challenge.max_attempts,
                })

                if challenge.is_blocked():
                    logger.warning(
                        "OTP: челлендж %s ЗАБЛОКИРОВАН (%d/%d попыток)",
                        challenge_id[:8],
                        challenge.attempt_count,
                        challenge.max_attempts,
                    )
                else:
                    logger.info(
                        "OTP: неверный код для челленджа %s (попытка %d/%d)",
                        challenge_id[:8],
                        challenge.attempt_count,
                        challenge.max_attempts,
                    )
                return False

    # ── Управление челленджами ────────────────────────────────────────────

    async def resend_code(
        self,
        challenge_id: str,
        bot: Any,
        owner_chat_id: int,
    ) -> bool:
        """Повторно отправить код владельцу (если челлендж ещё активен).

        Код НЕ хранится — повторно отправить можно ТОЛЬКО если
        plaintext код ещё в памяти (временное окно).
        """
        async with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                logger.warning("OTP: повторная отправка — челлендж %s не найден", challenge_id[:8])
                return False

            if challenge.is_expired() or challenge.used:
                logger.warning("OTP: повторная отправка — челлендж %s неактивен", challenge_id[:8])
                return False

            # Если код уже удалён из памяти — не можем отправить повторно
            code = self._plaintext_codes.get(challenge_id)
            if code is None:
                logger.warning(
                    "OTP: повторная отправка невозможна — код уже удалён из памяти для %s",
                    challenge_id[:8],
                )
                return False

        # Отправляем заново (вне блокировки)
        try:
            await self._send_code_via_bot(
                bot=bot,
                chat_id=owner_chat_id,
                code=code,
                description=challenge.action_description,
                resend=True,
            )
            logger.info("OTP: код повторно отправлен для челленджа %s", challenge_id[:8])
            return True
        except Exception as exc:
            logger.error("OTP: ошибка повторной отправки кода: %s", exc)
            return False

    def get_pending_challenge(
        self, user_id: int, task_id: str
    ) -> OTPChallenge | None:
        """Найти активный челлендж для пользователя + задачи.

        Args:
            user_id: ID пользователя.
            task_id: ID задачи.

        Returns:
            OTPChallenge или None если активного челленджа нет.
        """
        for challenge in self._challenges.values():
            if (
                challenge.user_id == user_id
                and challenge.task_id == task_id
                and not challenge.used
                and not challenge.is_expired()
                and not challenge.is_blocked()
            ):
                return challenge
        return None

    def cleanup_expired(self) -> int:
        """Удалить все просроченные челленджи.

        Returns:
            Количество удалённых челленджей.
        """
        now = time.time()
        expired_ids = [
            cid
            for cid, ch in self._challenges.items()
            if ch.is_expired() or (ch.used and now - ch.created_at > 3600)
        ]
        for cid in expired_ids:
            del self._challenges[cid]
            self._plaintext_codes.pop(cid, None)

        if expired_ids:
            self._save()
            logger.info("OTP: очищено %d просроченных челленджей", len(expired_ids))

        return len(expired_ids)

    # ── Dashboard login (упрощённая версия без bot instance) ──────────────

    async def create_dashboard_challenge(
        self,
        user_id: int,
        action_type: str,
        bot_token: str,
        chat_id: int,
    ) -> OTPChallenge:
        """Создать OTP-челлендж для входа в Dashboard и отправить код через Telegram API.

        Упрощённая версия create_challenge для dashboard login — не требует
        task_id, risk_level и bot instance. Отправляет код напрямую через
        Telegram Bot API (httpx).

        Args:
            user_id: Telegram user ID владельца.
            action_type: Тип действия (например "dashboard.login").
            bot_token: Telegram Bot API токен.
            chat_id: ID чата для отправки кода.

        Returns:
            OTPChallenge — созданный челлендж.
        """
        import httpx

        code = self._generate_code()
        salt = secrets.token_hex(16)
        code_hash = self._hash_code(code, salt)

        now = time.time()
        challenge = OTPChallenge(
            challenge_id=uuid.uuid4().hex,
            user_id=user_id,
            task_id="",
            action_description=f"Вход в Dashboard (user_id={user_id})",
            action_type=action_type,
            risk_level="HIGH",
            salt=salt,
            code_hash=code_hash,
            created_at=now,
            expires_at=now + OTP_CODE_LIFETIME,
            max_attempts=OTP_MAX_ATTEMPTS,
        )

        async with self._lock:
            self._challenges[challenge.challenge_id] = challenge
            self._plaintext_codes[challenge.challenge_id] = code
            self._save()

        # Отправка через Telegram Bot API (httpx вместо bot instance)
        try:
            message = (
                f"🔐 <b>Код для входа в Dashboard</b>\n\n"
                f"Код: <code>{code}</code>\n\n"
                f"⏳ Действителен {OTP_CODE_LIFETIME // 60} минуты\n"
                f"⚠️ Никому не сообщайте этот код"
            )
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                )
                if resp.is_success:
                    logger.info("OTP: dashboard-код отправлен в chat_id=%d", chat_id)
                else:
                    logger.warning(
                        "OTP: не удалось отправить dashboard-код: %s",
                        resp.text,
                    )
        except Exception as exc:
            logger.error("OTP: ошибка отправки dashboard-кода: %s", exc)

        self._emit_security_event({
            "event": "OTP_DASHBOARD_CHALLENGE_CREATED",
            "challenge_id": challenge.challenge_id[:8],
            "user_id": user_id,
            "action_type": action_type,
        })

        return challenge

    async def verify_dashboard_code(
        self, user_id: int, action_type: str, code_attempt: str
    ) -> tuple[bool, str]:
        """Проверить OTP-код для входа в Dashboard (по user_id + action_type).

        Находит активный челлендж по user_id + action_type и проверяет код.
        Возвращает (True, challenge_id) при успехе или (False, reason) при ошибке.

        Args:
            user_id: Telegram user ID владельца.
            action_type: Тип действия (например "dashboard.login").
            code_attempt: Введённый пользователем код.

        Returns:
            (True, challenge_id) если код верен, (False, reason) если нет.
        """
        # Ищем активный челлендж по user_id + action_type
        challenge_id: str | None = None
        async with self._lock:
            for cid, ch in self._challenges.items():
                if (ch.user_id == user_id
                        and ch.action_type == action_type
                        and not ch.used
                        and not ch.is_expired()
                        and not ch.is_blocked()):
                    challenge_id = cid
                    break

        if challenge_id is None:
            logger.warning(
                "OTP: активный dashboard-челлендж не найден для user=%d action=%s",
                user_id, action_type,
            )
            return False, "Код не запрашивался или истёк"

        # Проверяем код через стандартный verify_code
        is_valid = await self.verify_code(challenge_id, code_attempt)
        if is_valid:
            return True, challenge_id
        return False, "Неверный код"

    # ── Приватные методы ──────────────────────────────────────────────────

    @staticmethod
    def _generate_code() -> str:
        """Сгенерировать криптостойкий 6-значный код.

        Использует secrets.randbelow для защиты от угадывания.
        Никогда не записывается в логи.
        """
        code_int = secrets.randbelow(10**OTP_CODE_LENGTH)
        return str(code_int).zfill(OTP_CODE_LENGTH)

    @staticmethod
    def _hash_code(code: str, salt: str) -> str:
        """PBKDF2-HMAC-SHA256 хеш кода."""
        return hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 100_000).hex()

    @staticmethod
    async def _send_code_via_bot(
        bot: Any,
        chat_id: int,
        code: str,
        description: str,
        resend: bool = False,
    ) -> None:
        """Отправить OTP-код владельцу в Telegram.

        Код отправляется ТОЛЬКО в личный чат владельца.
        Код НЕ логируется (нарушение §19).

        Args:
            bot: Telegram Bot instance.
            chat_id: ID чата владельца.
            code: Plaintext OTP-код (не логируется).
            description: Описание действия.
            resend: True если это повторная отправка.
        """
        prefix = "🔐 *ПОВТОРНАЯ ОТПРАВКА КОДА*" if resend else "🔐 *ОДНОРАЗОВЫЙ КОД*"
        message = (
            f"{prefix}\n\n"
            f"Код: `{code}`\n"
            f"Действие: {description}\n\n"
            f"⏳ Код действителен {OTP_CODE_LIFETIME // 60} минуты\n"
            f"⚠️ Никому не сообщайте этот код\n"
            f"❌ Неверных попыток: до {OTP_MAX_ATTEMPTS}"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )

    def _emit_security_event(self, data: dict[str, Any]) -> None:
        """Опубликовать событие безопасности.

        Args:
            data: Данные события.
        """
        # Всегда логируем (без plaintext кодов)
        logger.info("SECURITY_EVENT: %s", json.dumps(data, ensure_ascii=False))

        # Опционально публикуем через внешний publisher
        if self._event_publisher is not None:
            try:
                self._event_publisher(data)
            except Exception as exc:
                logger.error(
                    "OTP: ошибка публикации SECURITY_EVENT: %s", exc
                )

    # ── Персистентность ───────────────────────────────────────────────────

    def _load(self) -> None:
        """Загрузить челленджи из JSON-файла."""
        if not self._persist_path.exists():
            self._challenges = {}
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            self._challenges = {
                cid: OTPChallenge.from_dict(ch)
                for cid, ch in data.items()
            }
            logger.debug("OTP: загружено %d челленджей", len(self._challenges))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("OTP: ошибка загрузки %s: %s", self._persist_path, exc)
            self._challenges = {}

    def _save(self) -> None:
        """Сохранить челленджи в JSON-файл.

        Сохраняется ТОЛЬКО хеш кода, НИКОГДА не plaintext.
        """
        try:
            data = {
                cid: ch.to_dict()
                for cid, ch in self._challenges.items()
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.chmod(self._persist_path, 0o600)
        except OSError as exc:
            logger.error(
                "OTP: ошибка сохранения %s: %s", self._persist_path, exc
            )

    def __repr__(self) -> str:
        return (
            f"<OTPManager challenges={len(self._challenges)} "
            f"persist={self._persist_path}>"
        )
