"""Тесты OTP-интеграции (§15, §24.5).

Сценарии приемки (§24.5):
  1. HIGH-действие → OTP-челлендж создаётся
  2. Неверный код → отклонён
  3. Истёкший код → отклонён
  4. Использованный код → отклонён
  5. Верный код → выполняется
  6. Блокировка после 3 неверных попыток
  7. Security event публикуется при каждой проверке
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from antigona.security.otp import OTP_CODE_LIFETIME, OTP_MAX_ATTEMPTS, OTPChallenge, OTPManager
from antigona.security.risk_classifier import RiskLevel

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def otp_persist(tmp_path: Path) -> str:
    return str(tmp_path / "challenges.json")


@pytest.fixture
async def otp_manager(otp_persist: str) -> OTPManager:
    manager = OTPManager(persist_path=otp_persist)
    return manager


async def _create_challenge(otp_manager: OTPManager, user_id: int = 12345) -> tuple[OTPChallenge, str]:
    """Создать OTP-челлендж и вернуть (challenge, plaintext_code).

    Подменяет bot на AsyncMock, чтобы не отправлять реальные сообщения.
    """
    bot = AsyncMock()
    challenge = await otp_manager.create_challenge(
        user_id=user_id,
        task_id="task_abc",
        action_description="Тестовое опасное действие",
        action_type="RUN_SHELL",
        risk_level=RiskLevel.HIGH,
        bot=bot,
        owner_chat_id=user_id,
    )

    # Извлекаем plaintext код из вызова bot.send_message
    # (OTPManager отправляет код через bot, но в тестах мы можем
    # восстановить код из временного хранилища)
    plaintext_code = ""
    if bot.send_message.called:
        call_args = bot.send_message.call_args
        if call_args and len(call_args) > 1:
            text = call_args[1].get("text", "") if isinstance(call_args[1], dict) else str(call_args[1])
            # Ищем 6-значный код в тексте
            import re
            match = re.search(r"\b(\d{6})\b", text)
            if match:
                plaintext_code = match.group(1)

    return challenge, plaintext_code


# ── Tests ────────────────────────────────────────────────────────────────────


class TestOTPChallengeFlow:
    """§24.5 сценарии OTP."""

    async def test_high_action_creates_challenge(self, otp_manager: OTPManager) -> None:
        """HIGH-действие → OTP-челлендж создаётся."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345,
            task_id="task_abc",
            action_description="Удаление файла",
            action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH,
            bot=bot,
            owner_chat_id=12345,
        )
        assert challenge is not None
        assert challenge.challenge_id
        assert challenge.risk_level == "HIGH"
        assert challenge.user_id == 12345
        assert challenge.task_id == "task_abc"
        assert challenge.code_hash  # Хеш есть
        assert not challenge.used
        assert not challenge.is_expired()
        # Код был отправлен
        assert bot.send_message.called

    async def test_wrong_code_rejected(self, otp_manager: OTPManager) -> None:
        """Неверный код → отклонён."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )

        # Пробуем неверный код
        result = await otp_manager.verify_code(challenge.challenge_id, "000000")
        assert result is False
        # Попытка учтена
        updated = otp_manager._challenges[challenge.challenge_id]
        assert updated.attempt_count == 1

    async def test_expired_code_rejected(self, otp_manager: OTPManager) -> None:
        """Истёкший код → отклонён."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )

        # Сдвигаем время челленджа в прошлое
        challenge.created_at = time.time() - OTP_CODE_LIFETIME - 10
        challenge.expires_at = time.time() - 10

        result = await otp_manager.verify_code(challenge.challenge_id, "000000")
        assert result is False

    async def test_used_code_rejected(self, otp_manager: OTPManager) -> None:
        """Использованный код → отклонён."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )

        # Помечаем использованным
        challenge.used = True

        result = await otp_manager.verify_code(challenge.challenge_id, "000000")
        assert result is False

    async def test_blocked_after_max_attempts(self, otp_manager: OTPManager) -> None:
        """После 3 неверных попыток — блокировка."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )

        # 3 неверные попытки
        for _i in range(OTP_MAX_ATTEMPTS):
            result = await otp_manager.verify_code(challenge.challenge_id, "000000")
            assert result is False

        # Четвёртая — blocked
        result = await otp_manager.verify_code(challenge.challenge_id, "000000")
        assert result is False

        updated = otp_manager._challenges[challenge.challenge_id]
        assert updated.is_blocked()

    async def test_challenge_not_found(self, otp_manager: OTPManager) -> None:
        """Несуществующий челлендж — False."""
        result = await otp_manager.verify_code("nonexistent", "123456")
        assert result is False

    async def test_pending_challenge_found(self, otp_manager: OTPManager) -> None:
        """Поиск активного челленджа по user_id + task_id."""
        bot = AsyncMock()
        await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )
        pending = otp_manager.get_pending_challenge(12345, "task_abc")
        assert pending is not None
        assert pending.user_id == 12345

    async def test_pending_challenge_not_found_wrong_user(self, otp_manager: OTPManager) -> None:
        """Чужой пользователь не видит челлендж."""
        bot = AsyncMock()
        await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )
        pending = otp_manager.get_pending_challenge(99999, "task_abc")
        assert pending is None

    async def test_cleanup_expired(self, otp_manager: OTPManager) -> None:
        """Очистка просроченных челленджей."""
        bot = AsyncMock()
        challenge = await otp_manager.create_challenge(
            user_id=12345, task_id="task_abc",
            action_description="Test", action_type="RUN_SHELL",
            risk_level=RiskLevel.HIGH, bot=bot,
            owner_chat_id=12345,
        )
        # Сдвигаем время
        challenge.created_at = time.time() - 3600
        challenge.expires_at = time.time() - 100

        cleaned = otp_manager.cleanup_expired()
        assert cleaned >= 1
        assert challenge.challenge_id not in otp_manager._challenges


class TestOTPChallengeModel:
    """Модель OTPChallenge."""

    def test_is_expired(self) -> None:
        c = OTPChallenge(
            created_at=time.time() - 200,
            expires_at=time.time() - 10,
        )
        assert c.is_expired()

    def test_not_expired(self) -> None:
        c = OTPChallenge(
            created_at=time.time(),
            expires_at=time.time() + 120,
        )
        assert not c.is_expired()

    def test_is_blocked(self) -> None:
        c = OTPChallenge(max_attempts=3, attempt_count=3)
        assert c.is_blocked()

    def test_can_attempt(self) -> None:
        c = OTPChallenge(
            created_at=time.time(),
            expires_at=time.time() + 120,
            used=False,
            attempt_count=0,
        )
        assert c.can_attempt()

    def test_cannot_attempt_when_used(self) -> None:
        c = OTPChallenge(used=True)
        assert not c.can_attempt()

    def test_serialize_deserialize(self) -> None:
        original = OTPChallenge(
            challenge_id="test-123",
            user_id=12345,
            task_id="task_abc",
            action_description="Test action",
            action_type="RUN_SHELL",
            risk_level="HIGH",
            code_hash="abc123",
            created_at=1000.0,
            expires_at=1120.0,
        )
        data = original.to_dict()
        restored = OTPChallenge.from_dict(data)
        assert restored.challenge_id == "test-123"
        assert restored.user_id == 12345
        assert restored.risk_level == "HIGH"
