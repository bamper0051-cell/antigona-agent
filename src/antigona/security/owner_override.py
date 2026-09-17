"""OwnerOverrideManager — Единый менеджер повышенных прав владельца Antigona.

Основные функции:
- PBKDF2/SHA-256 + 16-байтовая соль для хеширования PIN-кода (без захардкоженных PIN, запрет логирования PIN).
- Ограничение попыток: максимум 5 неверных попыток → 15 минут (900 сек) блокировки (lockout).
- Сессия повышенных прав: TTL 15 минут (900 сек), привязанная строго к тройке (channel, user_id, session_id).
- Подтверждение владельца Telegram: по ANTIGONA_TELEGRAM_OWNER_ID (int).
- Подтверждение владельца CLI: по локальному пользователю ОС / owner token.
- Аварийный сброс авторизации (lock_session / lock_all).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from antigona.security.elevation import ElevationAuthority

logger = logging.getLogger(__name__)

# Константы по умолчанию
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_TTL_SECONDS = 900.0  # 15 минут
DEFAULT_SESSION_TTL_SECONDS = 900.0  # 15 минут
PBKDF2_ITERATIONS = 100_000
SALT_BYTES = 16


class OwnerOverrideManager:
    """Единый менеджер авторизации и повышенных прав владельца.

    Гарантирует безопасную аутентификацию по PIN-коду, отслеживание блокировок,
    изоляцию сессий и быструю отмену прав (emergency lock).
    """

    def __init__(
        self,
        pin_file_path: Path | str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lockout_ttl: float = DEFAULT_LOCKOUT_TTL_SECONDS,
        session_ttl: float = DEFAULT_SESSION_TTL_SECONDS,
        telegram_owner_id: int | None = None,
        owner_token: str | None = None,
        elevation: ElevationAuthority | None = None,
    ) -> None:
        self._lock = threading.Lock()

        # Настройки пути к файлу хеша PIN
        if pin_file_path is not None:
            self._pin_file_path = Path(pin_file_path)
        else:
            env_path = os.environ.get("ANTIGONA_OWNER_PIN_FILE", "")
            if env_path:
                self._pin_file_path = Path(env_path)
            else:
                # Governed runtime resolver (owner_pin_file): never the
                # read-only code root; fail-closed in an immutable deployment.
                # The elevation store below is derived from the same directory,
                # so the PIN gate and the shared elevation authority stay on the
                # same durable store.
                from antigona.core.paths import owner_pin_file
                self._pin_file_path = owner_pin_file()


        self._max_attempts = max_attempts
        self._lockout_ttl = lockout_ttl
        self._session_ttl = session_ttl

        # Единый durable-стор блокировок и elevated-сессий (campaign CP-2).
        # Раньше это были RAM-поля _failed_attempts / _lockout_until /
        # _active_sessions — рестарт стирал brute-force lockout. Теперь состояние
        # переживает рестарт (SQLite рядом с owner_pin.json).
        self._elevation: ElevationAuthority = elevation or ElevationAuthority(
            db_path=self._pin_file_path.parent / "elevation.db",
            max_attempts=max_attempts,
            lockout_seconds=lockout_ttl,
            session_ttl_seconds=session_ttl,
        )
        self._lockout_principal = f"owner-override:{self._pin_file_path}"

        # Идентификатор владельца Telegram — единый источник истины:
        # OwnerIdentity (campaign CP-6). Раньше здесь был свой env-резолвер на
        # ANTIGONA_TELEGRAM_OWNER_ID | ANTIGONA_OWNER_ID; теперь оба алиаса
        # обрабатывает OwnerIdentity, а этот менеджер только берёт результат.
        self._telegram_owner_id: int | None = telegram_owner_id
        if self._telegram_owner_id is None:
            from antigona.security.owner_identity import OwnerIdentity

            self._telegram_owner_id = OwnerIdentity().owner_user_id

        # Токен владельца CLI
        self._owner_token: str | None = owner_token or os.environ.get("ANTIGONA_OWNER_TOKEN")

        # Имя ОС-пользователя, объявленного владельцем CLI. Если не задано —
        # владелец CLI не сконфигурирован и `is_cli_owner()` отвечает False.
        self._cli_owner_user: str = (
            os.environ.get("ANTIGONA_CLI_OWNER_USER") or ""
        ).strip()

        # Загрузка или инициализация PIN хеша из окружения или файла
        self._stored_salt: bytes | None = None
        self._stored_hash: bytes | None = None
        self._pin_file_mtime: float = 0.0
        self._load_pin_hash()

    def _session_principal(self, channel: str, user_id: str | int, session_id: str) -> str:
        # Campaign CP-7: the canonical key shape, shared with PolicyEngine's
        # elevation check, so verify_and_elevate here is visible there.
        from antigona.security.elevation import principal_for

        return principal_for(channel, user_id, session_id)

    # ── Загрузка / Сохранение PIN ─────────────────────────────────────────

    def _load_pin_hash(self) -> None:
        """Загрузить хеш PIN-кода из файла или отинициализировать из ANTIGONA_PIN."""
        if self._pin_file_path.exists():
            try:
                mtime = self._pin_file_path.stat().st_mtime
                data = json.loads(self._pin_file_path.read_text(encoding="utf-8"))
                if data.get("algorithm") == "pbkdf2_sha256":
                    self._stored_salt = bytes.fromhex(data["salt"])
                    self._stored_hash = bytes.fromhex(data["hash"])
                    self._pin_file_mtime = mtime
                    return
            except Exception as e:
                logger.error("Ошибка чтения файла PIN-хеша %s: %s", self._pin_file_path, e)

        # Резервный вариант — ANTIGONA_PIN из окружения (для тестов/первичного сида)
        env_pin = os.environ.get("ANTIGONA_PIN", "")
        if env_pin:
            self._set_pin_internal(env_pin, save_to_disk=False)

    def reload(self) -> None:
        """Перезагрузить состояние из файла и сбросить счетчики блокировки."""
        with self._lock:
            self._load_pin_hash()
            self._elevation.reset(self._lockout_principal)

    def _check_disk_sync(self) -> None:
        """Проверить, не изменился ли файл PIN-хеша на диске."""
        if self._pin_file_path.exists():
            try:
                mtime = self._pin_file_path.stat().st_mtime
                if mtime > self._pin_file_mtime:
                    old_hash = self._stored_hash
                    self._load_pin_hash()
                    if self._stored_hash != old_hash:
                        self._elevation.reset(self._lockout_principal)
            except Exception:
                pass

    def set_pin(self, new_pin: str) -> None:
        """Установить новый PIN-код, сгенерировав соль и сохранив PBKDF2 хеш на диск.

        ВНИМАНИЕ: Запрещено логировать сырой PIN!
        """
        if not new_pin:
            raise ValueError("PIN-код не может быть пустым.")
        with self._lock:
            self._set_pin_internal(new_pin, save_to_disk=True)
            self._elevation.reset(self._lockout_principal)
            logger.info("OwnerOverrideManager: PIN-код успешно изменён.")

    def _set_pin_internal(self, pin: str, save_to_disk: bool = True) -> None:
        salt = secrets.token_bytes(SALT_BYTES)
        hash_val = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        self._stored_salt = salt
        self._stored_hash = hash_val

        if save_to_disk:
            try:
                self._pin_file_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "algorithm": "pbkdf2_sha256",
                    "iterations": PBKDF2_ITERATIONS,
                    "salt": salt.hex(),
                    "hash": hash_val.hex(),
                }
                self._pin_file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                # Устанавливаем безопасные права на файл (600)
                os.chmod(self._pin_file_path, 0o600)
                self._pin_file_mtime = self._pin_file_path.stat().st_mtime
            except Exception as e:
                logger.error("Ошибка сохранения PIN-хеша в %s: %s", self._pin_file_path, e)

    # ── Проверка PIN и Блокировки (Lockout) ──────────────────────────────

    def is_locked_out(self, now: float | None = None) -> bool:
        """Проверить, находится ли система в состоянии блокировки после 5 попыток."""
        current_time = time.time() if now is None else now
        with self._lock:
            self._check_disk_sync()
            return self._elevation.is_locked_out(self._lockout_principal, now=current_time)

    def get_remaining_lockout_seconds(self, now: float | None = None) -> float:
        """Получить оставшееся время блокировки в секундах."""
        current_time = time.time() if now is None else now
        return self._elevation.remaining_lockout(self._lockout_principal, now=current_time)

    def get_failed_attempts(self) -> int:
        """Получить текущее количество неверных попыток."""
        return self._elevation.failed_attempts(self._lockout_principal)

    def reset_lockout(self) -> None:
        """Сбросить блокировку и счетчик неверных попыток."""
        with self._lock:
            self._elevation.reset(self._lockout_principal)
            logger.info("OwnerOverrideManager: Блокировка попыток сброшена.")

    def verify_pin(self, pin: str, now: float | None = None) -> bool:
        """Проверить совпадение PIN-кода с PBKDF2 хешем с отслеживанием 5 неверных попыток.

        ВНИМАНИЕ: Запрещено логировать аргумент `pin`.
        """
        current_time = time.time() if now is None else now

        with self._lock:
            self._check_disk_sync()
            # Блокировка (durable — переживает рестарт, campaign CP-2)
            if self._elevation.is_locked_out(self._lockout_principal, now=current_time):
                logger.warning(
                    "OwnerOverrideManager: Отклонён запрос PIN — активна блокировка."
                )
                return False

            # Проверка конфигурации PIN
            if self._stored_salt is None or self._stored_hash is None:
                logger.warning("OwnerOverrideManager: PIN не сконфигурирован (fail-closed).")
                return False

            # Вычисление PBKDF2 хеша от предложенного PIN
            candidate_hash = hashlib.pbkdf2_hmac(
                "sha256", pin.encode("utf-8"), self._stored_salt, PBKDF2_ITERATIONS
            )

            # Сравнение за постоянное время
            if secrets.compare_digest(candidate_hash, self._stored_hash):
                self._elevation.record_success(self._lockout_principal)
                logger.info("OwnerOverrideManager: Проверка PIN успешна.")
                return True
            locked_now, remaining = self._elevation.record_failure(
                self._lockout_principal, now=current_time
            )
            logger.warning(
                "OwnerOverrideManager: Неверный PIN (осталось попыток: %d)", remaining
            )
            if locked_now:
                logger.error(
                    "OwnerOverrideManager: Превышено число неверных попыток. Блокировка на %.0f сек.",
                    self._lockout_ttl,
                )
            return False

    # ── Сессии повышенных прав (Elevated Sessions) ─────────────────────────

    def elevate_session(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> None:
        """Создать или продлить сессию повышенных прав, привязанную к (channel, user_id, session_id)."""
        current_time = time.time() if now is None else now
        ttl = self._session_ttl if ttl_seconds is None else ttl_seconds

        with self._lock:
            self._elevation.elevate(
                self._session_principal(channel, user_id, session_id),
                ttl=ttl,
                now=current_time,
            )
            logger.info(
                "OwnerOverrideManager: Сессия повышенных прав активирована (channel=%s, user_id=%s, session_id=%s, ttl=%.0fs)",
                channel,
                user_id,
                session_id,
                ttl,
            )

    def is_elevated(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        now: float | None = None,
    ) -> bool:
        """Проверить, активна ли сессия повышенных прав для указанной тройки."""
        current_time = time.time() if now is None else now
        return self._elevation.is_elevated(
            self._session_principal(channel, user_id, session_id), now=current_time
        )

    def verify_and_elevate(
        self,
        channel: str,
        user_id: str | int,
        session_id: str,
        pin: str,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Проверить PIN и при успехе активировать сессию повышенных прав на 15 минут.

        Returns:
            (success, message)
        """
        current_time = time.time() if now is None else now

        if self.is_locked_out(current_time):
            rem = self.get_remaining_lockout_seconds(current_time)
            return (
                False,
                f"Доступ заблокирован из-за множества неверных попыток. Попробуйте через {int(rem)} сек.",
            )

        if self.verify_pin(pin, now=current_time):
            self.elevate_session(channel, user_id, session_id, now=current_time)
            return (True, "Сессия повышенных прав успешно активирована.")

        if self.is_locked_out(current_time):
            return (
                False,
                "Превышено число неверных попыток. Система заблокирована на 15 минут.",
            )

        attempts = self.get_failed_attempts()
        rem_attempts = self._max_attempts - attempts
        return (
            False,
            f"Неверный PIN-код. Осталось попыток: {rem_attempts}.",
        )

    # ── Проверка владельцев Telegram & CLI ─────────────────────────────

    def is_telegram_owner(self, user_id: int | str) -> bool:
        """Проверить, является ли user_id подтверждённым владельцем Telegram."""
        if self._telegram_owner_id is None:
            return False
        try:
            val = int(str(user_id).strip())
            return val == self._telegram_owner_id
        except ValueError:
            return False

    def is_cli_owner(
        self, os_user: str | None = None, owner_token: str | None = None
    ) -> bool:
        """Проверить, является ли пользователь владельцем CLI.

        Владелец CLI подтверждается ТОЛЬКО одним из двух способов:

        * ``owner_token`` совпадает с ``ANTIGONA_OWNER_TOKEN``;
        * текущий ОС-пользователь совпадает с ``ANTIGONA_CLI_OWNER_USER``.

        Если ни токен, ни ``ANTIGONA_CLI_OWNER_USER`` не заданы — владелец CLI
        не сконфигурирован и метод возвращает ``False`` (fail-closed). Запуск
        от root больше НЕ считается подтверждением владельца.
        """
        # Если явно передан токен владельца
        if owner_token is not None:
            if self._owner_token is not None and secrets.compare_digest(
                owner_token, self._owner_token
            ):
                return True
            return False

        # Определение текущего пользователя ОС
        try:
            current_os_user = getpass.getuser()
        except Exception:
            current_os_user = ""

        # Если явно передан os_user — проверяем строгое совпадение с
        # сконфигурированным владельцем (не просто с текущим ОС-пользователем):
        # иначе любой вызывающий, передавший текущий username, получает owner.
        if os_user is not None:
            if not self._cli_owner_user or not current_os_user:
                return False
            return os_user == self._cli_owner_user and os_user == current_os_user

        # Без аргументов — сверяем ОС-пользователя с сконфигурированным владельцем.
        if not self._cli_owner_user or not current_os_user:
            logger.warning(
                "OwnerOverrideManager: ANTIGONA_CLI_OWNER_USER не задан — "
                "владелец CLI не подтверждён (fail-closed)"
            )
            return False
        return secrets.compare_digest(current_os_user, self._cli_owner_user)

    # ── Аварийный сброс авторизации (Emergency Lock) ──────────────────────

    def lock_session(self, channel: str, user_id: str | int, session_id: str) -> bool:
        """Сбросить авторизацию конкретной сессии (/lock)."""
        with self._lock:
            revoked = self._elevation.revoke(
                self._session_principal(channel, user_id, session_id)
            )
            if revoked:
                logger.info(
                    "OwnerOverrideManager: Сброшена авторизация сессии (channel=%s, user_id=%s, session_id=%s)",
                    channel,
                    user_id,
                    session_id,
                )
            return revoked

    def lock_all(self) -> int:
        """Сбросить авторизацию абсолютно всех сессий (emergency lock)."""
        with self._lock:
            count = self._elevation.revoke_all()
            logger.info("OwnerOverrideManager: Сброшены все сессии (%d сессий заблокировано).", count)
            return count

    def __repr__(self) -> str:
        with self._lock:
            has_pin = self._stored_hash is not None
            locked_out = self._elevation.is_locked_out(self._lockout_principal)
        return (
            f"<OwnerOverrideManager has_pin={has_pin} "
            f"locked_out={locked_out} telegram_owner={self._telegram_owner_id}>"
        )
