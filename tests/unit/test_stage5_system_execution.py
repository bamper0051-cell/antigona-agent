"""Авто-тесты Этапа 5: Безопасное выполнение системных действий и аудиторский журнал Antigona.

Покрывает:
1. Интеграция OwnerOverrideManager + PolicyEngine + ActionExecutor:
   - Снятие глухих отказов политики для SAFE и SENSITIVE системных действий при активном Owner Override.
   - 2-ступенчатое целевое подтверждение для CRITICAL-действий (rm -rf, DROP_DATABASE, systemctl stop/restart, FIREWALL_CHANGE, MASS_KILL).
   - Отображение точной команды в fenced code block и ожидаемого эффекта.
   - Запрос точной подтверждающей фразы или 1-разового токена `/confirm <token>`.
   - Отклонение простых "да" / "yes" / "ok" и несовпадающих токенов.
2. Безопасный Audit Log:
   - Запись каждого системного действия с (channel, user_id, session_id, command, exit_code, timestamp).
   - Запрет записи сырых PIN, токенов и секретов в логи и БД (маскировка).
3. Таймауты и отмена:
   - Поддержка отмены (/cancel) с честным exit_code=130 и вывода.
   - Таймауты выполнения с честным exit_code=124 и вывода (запрет подделки отчетов об успехе).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

from antigona.core.paths import home_dir
from antigona.policy.engine import PolicyEngine
from antigona.security.audit import SystemAuditLogger, sanitize_secrets
from antigona.security.owner_override import OwnerOverrideManager
from antigona.tools.action_executor import Action, ActionExecutor, ActionType

# Derived from the canonical home helper instead of a hardcoded owner path.
_HOME = str(home_dir())


@pytest.fixture
def tmp_pin_file() -> Path:
    """Временный файл для PIN-хеша."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def tmp_audit_db() -> Path:
    """Временная SQLite БД для аудита."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def owner_override(tmp_pin_file: Path, monkeypatch: pytest.MonkeyPatch) -> OwnerOverrideManager:
    import getpass

    monkeypatch.setenv("ANTIGONA_CLI_OWNER_USER", getpass.getuser())
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.set_pin("123456")
    return manager


@pytest.fixture
def audit_logger(tmp_audit_db: Path) -> SystemAuditLogger:
    return SystemAuditLogger(db_path=tmp_audit_db)


@pytest.fixture
def policy_engine(owner_override: OwnerOverrideManager) -> PolicyEngine:
    return PolicyEngine(owner_override=owner_override, require_approval=True)


@pytest.fixture
def action_executor(
    owner_override: OwnerOverrideManager,
    policy_engine: PolicyEngine,
    audit_logger: SystemAuditLogger,
) -> ActionExecutor:
    return ActionExecutor(
        owner_override=owner_override,
        policy_engine=policy_engine,
        audit_logger=audit_logger,
    )


# ── 1. Интеграция OwnerOverride + PolicyEngine + ActionExecutor ─────────────


class TestOwnerOverridePolicyIntegration:
    """Тесты интеграции OwnerOverride, PolicyEngine и ActionExecutor."""

    @pytest.mark.asyncio
    async def test_elevated_owner_override_does_not_bypass_high_write_approval(
        self,
        owner_override: OwnerOverrideManager,
        action_executor: ActionExecutor,
    ) -> None:
        channel = "telegram"
        user_id = "999888777"
        session_id = "session_stage5_safe"

        # Активируем Owner Override сессию
        owner_override.elevate_session(channel, user_id, session_id, ttl_seconds=900)
        assert owner_override.is_elevated(channel, user_id, session_id) is True

        # 1. SAFE действие (echo / read)
        safe_action = Action(
            type=ActionType.RUN_SHELL,
            command="echo 'Safe operation passed'",
        )
        res_safe = await action_executor._async_execute(
            safe_action, channel=channel, user_id=user_id, session_id=session_id
        )
        assert res_safe.success is True
        assert "Safe operation passed" in res_safe.message

        # 2. SENSITIVE действие (git / apt / docker / write file in workspace)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            system_zone = Path(_HOME) if sys.platform != "win32" else Path(os.environ.get("SystemRoot", "C:\\Windows"))
            target_path = system_zone / Path(tmpdir).name / "workspace_file.txt"
            write_action = Action(
                type=ActionType.WRITE_FILE,
                path=str(target_path),
                content="sensitive content in workspace",
            )
            res_write = await action_executor._async_execute(
                write_action, channel=channel, user_id=user_id, session_id=session_id
            )
            assert res_write.success is False
            assert res_write.error == "POLICY_DENIAL"
            assert "requires approval grant" in res_write.message
            assert not target_path.exists()

    @pytest.mark.asyncio
    async def test_critical_actions_require_2step_confirmation_even_when_elevated(
        self,
        owner_override: OwnerOverrideManager,
        policy_engine: PolicyEngine,
        action_executor: ActionExecutor,
    ) -> None:
        channel = "cli"
        user_id = "owner_user"
        session_id = "session_critical_test"

        # Включаем Owner Override
        owner_override.elevate_session(channel, user_id, session_id)
        assert owner_override.is_elevated(channel, user_id, session_id) is True

        critical_commands = [
            f"rm -rf {_HOME}/important_data",
            "DROP DATABASE production_db;",
            "systemctl stop nginx",
            "systemctl restart postgresql",
            "iptables -F",
            "kill -9 9876",
        ]

        for cmd in critical_commands:
            action = Action(type=ActionType.RUN_SHELL, command=cmd)
            res = await action_executor._async_execute(
                action, channel=channel, user_id=user_id, session_id=session_id
            )

            # Должно требовать 2-ступенчатого подтверждения
            assert res.success is False
            assert res.error == "2STEP_CONFIRMATION_REQUIRED"
            assert "fenced code block" in res.message or "```" in res.message
            assert "Ожидаемый эффект" in res.message
            assert "/confirm" in res.message
            assert "Простые варианты 'да'/'yes'/'ok' отклоняются" in res.message

    def test_2step_confirmation_rejection_and_acceptance(
        self,
        policy_engine: PolicyEngine,
    ) -> None:
        channel = "telegram"
        user_id = "user123"
        session_id = "sess_confirm"

        # Создаём заявку на подтверждение
        pending = policy_engine.create_2step_confirmation(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command=f"rm -rf {_HOME}/example_dir",
            path=f"{_HOME}/example_dir",
        )

        assert pending.token is not None
        assert len(pending.token) >= 16  # token_urlsafe(16) → 22 chars
        assert "```" in pending.format_prompt()
        assert "Ожидаемый эффект:" in pending.format_prompt()

        # 1. Отклонение простых фраз
        for simple in ["да", "yes", "ok", "y", "1", "sure", "confirm", "подтверждаю"]:
            ok, msg, p = policy_engine.verify_confirmation(
                channel, user_id, session_id, simple
            )
            assert ok is False
            assert "не принимаются" in msg

        # 2. Отклонение неверных токенов и фраз
        ok, msg, p = policy_engine.verify_confirmation(
            channel, user_id, session_id, "/confirm wrongtok"
        )
        assert ok is False
        assert "несовпадающая" in msg

        # 3. Успешное подтверждение по токену
        ok, msg, p = policy_engine.verify_confirmation(
            channel, user_id, session_id, f"/confirm {pending.token}"
        )
        assert ok is True
        assert p is not None
        assert p.command == f"rm -rf {_HOME}/example_dir"

        # Повторное использование 1-разового токена отклоняется
        ok_again, _, _ = policy_engine.verify_confirmation(
            channel, user_id, session_id, f"/confirm {pending.token}"
        )
        assert ok_again is False

    def test_2step_confirmation_accepts_exact_phrase(
        self,
        policy_engine: PolicyEngine,
    ) -> None:
        channel = "cli"
        user_id = "owner"
        session_id = "sess_phrase"

        pending = policy_engine.create_2step_confirmation(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command="systemctl stop nginx",
            path="nginx",
        )

        exact_phrase = pending.exact_phrase
        assert exact_phrase == "Подтверждаю изменение сервиса nginx"

        # Подтверждаем точной фразой
        ok, msg, p = policy_engine.verify_confirmation(
            channel, user_id, session_id, exact_phrase
        )
        assert ok is True
        assert p is not None
        assert p.token == pending.token


# ── 2. Безопасный Audit Log ──────────────────────────────────────────────────


class TestSecureAuditLog:
    """Тесты аудиторского журнала и защиты от утёчки секретов."""

    def test_sanitize_secrets_redacts_pins_tokens_passwords(self) -> None:
        sample_command = "ANTIGONA_PIN=987654 /pin 123456 /confirm a1b2c3d4 sk-" + "proj-1234567890abcdef12345678 password=mysecretpassword123"
        cleaned = sanitize_secrets(sample_command)

        assert "123456" not in cleaned
        assert "987654" not in cleaned
        assert "a1b2c3d4" not in cleaned
        assert "sk-" + "proj-1234567890abcdef12345678" not in cleaned
        assert "mysecretpassword123" not in cleaned

        assert "[REDACTED_PIN]" in cleaned
        assert "[REDACTED_TOKEN]" in cleaned
        assert "[REDACTED_SECRET]" in cleaned

    def test_audit_logger_records_and_retrieves_entries(
        self,
        audit_logger: SystemAuditLogger,
    ) -> None:
        channel = "telegram"
        user_id = "112233"
        session_id = "session_audit"

        entry = audit_logger.log_action(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            command="git pull origin main --token=ghp_" + "abcdef1234567890abcdef12345678",
            exit_code=0,
            status="SUCCESS",
        )

        assert entry["id"] is not None
        assert "ghp_" + "abcdef1234567890abcdef12345678" not in entry["command"]
        assert "[REDACTED_TOKEN]" in entry["command"]

        # Запрос записей из SQLite БД
        logs = audit_logger.get_logs(channel=channel, session_id=session_id)
        assert len(logs) == 1
        assert logs[0]["user_id"] == user_id
        assert logs[0]["exit_code"] == 0
        assert logs[0]["status"] == "SUCCESS"
        assert "ghp_" + "abcdef1234567890abcdef12345678" not in logs[0]["command"]


# ── 3. Таймауты и Отмена (/cancel) ──────────────────────────────────────────


class TestTimeoutsAndCancellation:
    """Тесты честных exit_code для таймаутов и отмены."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="'sleep' unavailable on Windows (Wave 4)")
    async def test_command_timeout_returns_honest_exit_code_124(
        self,
        action_executor: ActionExecutor,
        audit_logger: SystemAuditLogger,
    ) -> None:
        channel = "cli"
        user_id = "owner"
        session_id = "session_timeout"

        action = Action(
            type=ActionType.RUN_SHELL,
            command="sleep 5",
        )

        # Выполняем с жестким таймаутом 0.2с
        res = await action_executor._async_execute(
            action, channel=channel, user_id=user_id, session_id=session_id, timeout=0.2
        )

        assert res.success is False
        assert res.error == "TIMEOUT"
        assert "превысила таймаут" in res.message

        # Проверяем честную запись в аудит-логе с exit_code 124
        logs = audit_logger.get_logs(channel=channel, session_id=session_id)
        assert len(logs) == 1
        assert logs[0]["exit_code"] == 124
        assert logs[0]["status"] == "TIMEOUT"

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="'sleep' unavailable on Windows (Wave 4)")
    async def test_command_cancellation_returns_honest_exit_code_130(
        self,
        action_executor: ActionExecutor,
        audit_logger: SystemAuditLogger,
    ) -> None:
        channel = "cli"
        user_id = "owner"
        session_id = "session_cancel"

        action = Action(
            type=ActionType.RUN_SHELL,
            command="sleep 10",
        )

        # Запускаем команду в отдельной async таске и вызываем cancel_session
        task = asyncio.create_task(
            action_executor._async_execute(
                action, channel=channel, user_id=user_id, session_id=session_id, timeout=10.0
            )
        )

        # Даём процессу запуститься
        await asyncio.sleep(0.1)

        # Симулируем /cancel
        cancelled = action_executor.cancel_session(session_id)
        assert cancelled is True

        res = await task
        assert res.success is False
        assert res.error == "CANCELLED"
        assert "отменено" in res.message.lower()

        # Проверяем аудит лог: exit_code=130, status="CANCELLED"
        logs = audit_logger.get_logs(channel=channel, session_id=session_id)
        assert len(logs) == 1
        assert logs[0]["exit_code"] == 130
        assert logs[0]["status"] == "CANCELLED"
