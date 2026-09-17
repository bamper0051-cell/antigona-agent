"""TOTPManager — TOTP-аутентификация через authenticator-приложения (§16).

Архитектура:
  - Совместимость со стандартными authenticator-приложениями (Google Authenticator,
    Authy, 1Password, Bitwarden и т.д.)
  - Секрет хранится в .env: ANTIGONA_TOTP_SECRET (base32)
  - Поддержка временного drift (±1 шаг = ±30 секунд)
  - Запрет повторного использования кода (replay prevention)
  - Безопасная процедура сброса/генерации
  - pyotp — опциональная зависимость (graceful fallback)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Опциональный импорт pyotp ─────────────────────────────────────────────

try:
    import pyotp as _pyotp_module

    _HAS_PYOTP = True
except ImportError:
    _pyotp_module = None  # type: ignore[assignment]
    _HAS_PYOTP = False

# Runtime alias — pyright sees the real import path
pyotp = _pyotp_module

# ── Константы ──────────────────────────────────────────────────────────────

def _security_dir() -> Path:
    """Runtime security persistence dir (governed, never the code root)."""
    from antigona.core import paths

    return paths.security_dir()


SECURITY_DIR = _security_dir()
TOTP_SECRET_ENV_KEY = "ANTIGONA_TOTP_SECRET"
TOTP_DRIFT_STEPS = 1          # Допустимый drift (±1 шаг = ±30 сек)
TOTP_REPLAY_WINDOW = 300      # Окно запрета повторного использования (5 мин)
TOTP_DIGITS = 6               # Количество цифр в TOTP-коде
TOTP_INTERVAL = 30            # Интервал генерации кода (стандарт RFC 6238)


# ── TOTP Manager ────────────────────────────────────────────────────────────


class TOTPManager:
    """Управление TOTP-аутентификацией через authenticator-приложения.

    Usage::

        manager = TOTPManager()

        # Проверка доступности
        if manager.is_configured():
            # Верификация кода
            if manager.verify(code="123456"):
                print("Код верен!")

        # Первоначальная настройка (только для владельца)
        if not manager.is_configured():
            secret = manager.generate_secret()
            uri = manager.get_provisioning_uri(label="Antigona")
            print(f"Добавьте в authenticator: {uri}")
            # Сохраните secret в .env как ANTIGONA_TOTP_SECRET

    Args:
        persist_path: Путь к файлу использованных кодов (replay prevention).
                      По умолчанию .security/totp_used_codes.json.
        secret_env_key: Ключ переменной окружения для TOTP-секрета.
        event_publisher: Опциональный callable для публикации SECURITY_EVENT.
                        Вызывается с dict-данными события.
    """

    def __init__(
        self,
        persist_path: str | Path | None = None,
        secret_env_key: str = TOTP_SECRET_ENV_KEY,
        event_publisher: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if persist_path is None:
            persist_path = SECURITY_DIR / "totp_used_codes.json"
        self._persist_path = Path(persist_path)
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)

        self._secret_env_key = secret_env_key
        self._event_publisher = event_publisher

        self._lock = threading.Lock()
        # Хранилище использованных кодов: code_hash -> timestamp
        self._used_codes: dict[str, float] = {}
        self._load_used_codes()

        if not _HAS_PYOTP:
            logger.warning(
                "pyotp не установлен — TOTP-аутентификация недоступна. "
                "Установите: pip install pyotp"
            )

        if self.is_configured():
            logger.info("TOTPManager инициализирован (TOTP настроен)")
        else:
            logger.info(
                "TOTPManager инициализирован (TOTP не настроен — "
                "ANTIGONA_TOTP_SECRET не задан или pyotp недоступен)"
            )

    # ── Проверка доступности ──────────────────────────────────────────────

    def is_configured(self) -> bool:
        """TOTP настроен и готов к работе?

        True если:
        - pyotp установлен
        - ANTIGONA_TOTP_SECRET задан в окружении
        """
        if not _HAS_PYOTP:
            return False
        secret = os.environ.get(self._secret_env_key, "")
        return bool(secret)

    def is_available(self) -> bool:
        """TOTP принципиально доступен (pyotp установлен)?

        Отличается от is_configured() — может быть True даже без секрета,
        если библиотека установлена.
        """
        return _HAS_PYOTP

    # ── Верификация ──────────────────────────────────────────────────────

    def verify(self, code: str) -> bool:
        """Проверить TOTP-код.

        §16 требования:
        - Проверка с учётом временного drift (±1 шаг)
        - Запрет повторного использования кода в критическом контексте
        - Аудит через SECURITY_EVENT

        Args:
            code: 6-значный код из authenticator-приложения.

        Returns:
            True если код верен и не использовался ранее.

        Note:
            Код НЕ логируется — это нарушение §19.
        """
        if not self.is_configured():
            logger.warning("TOTP: попытка верификации, но TOTP не настроен")
            self._emit_security_event({
                "event": "TOTP_VERIFY_FAILED",
                "reason": "NOT_CONFIGURED",
            })
            return False

        if not code or not code.strip():
            return False

        code = code.strip()

        # Проверка на повторное использование
        if self.has_recently_used(code):
            logger.warning("TOTP: попытка повторного использования кода")
            self._emit_security_event({
                "event": "TOTP_VERIFY_FAILED",
                "reason": "REPLAY_DETECTED",
            })
            return False

        # Чтение секрета из окружения
        secret = os.environ.get(self._secret_env_key, "")
        if not secret:
            return False

        try:
            totp = pyotp.TOTP(
                secret,
                digits=TOTP_DIGITS,
                interval=TOTP_INTERVAL,
            )
            # Проверка с учётом drift
            is_valid = totp.verify(
                code,
                valid_window=TOTP_DRIFT_STEPS,
            )
        except Exception as exc:
            logger.error("TOTP: ошибка верификации: %s", exc)
            self._emit_security_event({
                "event": "TOTP_VERIFY_ERROR",
                "reason": str(exc)[:100],
            })
            return False

        if is_valid:
            # Помечаем код как использованный (replay prevention)
            self._mark_used(code)
            self._emit_security_event({
                "event": "TOTP_VERIFIED",
                "reason": "SUCCESS",
            })
            logger.info("TOTP: код успешно подтверждён")
            return True
        else:
            self._emit_security_event({
                "event": "TOTP_VERIFY_FAILED",
                "reason": "WRONG_CODE",
            })
            logger.info("TOTP: неверный код")
            return False

    def has_recently_used(self, code: str, window: int = TOTP_REPLAY_WINDOW) -> bool:
        """Проверить, использовался ли код в последние N секунд.

        Предотвращает повторное использование одного и того же кода
        в течение заданного окна (replay attack protection).

        Args:
            code: TOTP-код для проверки.
            window: Окно в секундах (по умолчанию 300 = 5 минут).

        Returns:
            True если код уже использовался в данном окне.
        """
        code_hash = self._hash_totp_code(code)
        with self._lock:
            ts = self._used_codes.get(code_hash)
        if ts is None:
            return False
        return (time.time() - ts) < window

    # ── Управление секретом ──────────────────────────────────────────────

    def get_provisioning_uri(self, label: str = "Antigona") -> str:
        """Получить URI для настройки в authenticator-приложении.

        Создаёт URI вида otpauth://totp/Antigona?secret=XXXX&issuer=Antigona
        для сканирования QR-кода или ручного ввода.

        Args:
            label: Метка для authenticator-приложения.

        Returns:
            URI для настройки.

        Raises:
            RuntimeError: Если pyotp не установлен или секрет не задан.
        """
        if not _HAS_PYOTP:
            raise RuntimeError(
                "pyotp не установлен. Выполните: pip install pyotp"
            )
        secret = os.environ.get(self._secret_env_key, "")
        if not secret:
            raise RuntimeError(
                f"TOTP secret не задан. Установите {TOTP_SECRET_ENV_KEY} в .env"
            )
        totp = pyotp.TOTP(
            secret,
            digits=TOTP_DIGITS,
            interval=TOTP_INTERVAL,
            name=label,
            issuer="Antigona",
        )
        return totp.provisioning_uri(name=label, issuer_name="Antigona")

    def generate_secret(self) -> str:
        """Сгенерировать новый TOTP-секрет (base32).

        Генерирует новый криптостойкий секрет длиной 32 символа (base32).

        Returns:
            Новый секрет в формате base32.

        Note:
            Секрет нужно вручную сохранить в .env как ANTIGONA_TOTP_SECRET.
            После генерации старый секрет перестанет работать.
        """
        if not _HAS_PYOTP:
            raise RuntimeError(
                "pyotp не установлен. Выполните: pip install pyotp"
            )
        secret = pyotp.random_base32()
        logger.info("TOTP: сгенерирован новый секрет (сохраните в .env!)")
        return secret

    # ── Вспомогательные методы ───────────────────────────────────────────

    @staticmethod
    def _hash_totp_code(code: str) -> str:
        """Хешировать TOTP-код для хранения (replay prevention).

        Использует простой хеш — криптостойкость здесь не критична,
        так как TOTP-код действителен всего 30 секунд.
        """
        import hashlib

        return hashlib.sha256(code.encode()).hexdigest()[:16]

    def _mark_used(self, code: str) -> None:
        """Пометить TOTP-код как использованный.

        Args:
            code: TOTP-код.
        """
        code_hash = self._hash_totp_code(code)
        with self._lock:
            self._used_codes[code_hash] = time.time()
            self._save_used_codes()

    def _emit_security_event(self, data: dict[str, Any]) -> None:
        """Опубликовать событие безопасности.

        Args:
            data: Данные события.
        """
        logger.info("SECURITY_EVENT: TOTP %s", json.dumps(data, ensure_ascii=False))
        if self._event_publisher is not None:
            try:
                self._event_publisher(data)
            except Exception as exc:
                logger.error(
                    "TOTP: ошибка публикации SECURITY_EVENT: %s", exc
                )

    # ── Персистентность (replay prevention) ──────────────────────────────

    def _load_used_codes(self) -> None:
        """Загрузить использованные коды из JSON."""
        if not self._persist_path.exists():
            with self._lock:
                self._used_codes = {}
            return
        try:
            with open(self._persist_path) as f:
                codes = json.load(f)
            # Очистка устаревших записей
            now = time.time()
            stale = [
                h
                for h, ts in codes.items()
                if (now - ts) > TOTP_REPLAY_WINDOW * 2
            ]
            for h in stale:
                del codes[h]
            with self._lock:
                self._used_codes = codes
                if stale:
                    self._save_used_codes()
            logger.debug(
                "TOTP: загружено %d использованных кодов (очищено %d)",
                len(codes),
                len(stale),
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "TOTP: ошибка загрузки %s: %s", self._persist_path, exc
            )
            with self._lock:
                self._used_codes = {}

    def _save_used_codes(self) -> None:
        """Сохранить использованные коды в JSON."""
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(self._used_codes, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._persist_path)
        except OSError as exc:
            logger.error(
                "TOTP: ошибка сохранения %s: %s", self._persist_path, exc
            )

    def __repr__(self) -> str:
        configured = self.is_configured()
        return (
            f"<TOTPManager configured={configured} "
            f"pyotp={_HAS_PYOTP}>"
        )
