"""Авто-тесты для OwnerOverrideManager и recovery-скрипта (Этап 4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from antigona.bin.recovery_pin import run_recovery_pin_reset
from antigona.security.owner_override import OwnerOverrideManager


@pytest.fixture
def tmp_pin_file(tmp_path: Path) -> Path:
    return tmp_path / "test_owner_pin.json"


def test_verify_pin_success_and_wrong_pin(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.set_pin("987654")

    assert manager.verify_pin("987654") is True
    assert manager.verify_pin("000000") is False
    assert manager.verify_pin("") is False


def test_pin_hashing_pbkdf2_and_no_plaintext_logging(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    secret_pin = "super_secret_pin_123"
    manager.set_pin(secret_pin)

    # Проверка структуры сохранённого JSON
    assert tmp_pin_file.exists()
    content = tmp_pin_file.read_text(encoding="utf-8")
    assert secret_pin not in content

    data = json.loads(content)
    assert data["algorithm"] == "pbkdf2_sha256"
    assert data["iterations"] == 100_000
    assert len(data["salt"]) == 32  # 16 bytes in hex
    assert len(data["hash"]) == 64  # 32 bytes in hex

    # Проверка repr — не содержит PIN и секреты
    repr_str = repr(manager)
    assert secret_pin not in repr_str
    assert data["hash"] not in repr_str


def test_unconfigured_pin_fails_closed(
    tmp_pin_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTIGONA_PIN", raising=False)
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)

    assert manager.verify_pin("123456") is False


def test_lockout_after_5_failed_attempts(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file, max_attempts=5)
    manager.set_pin("112233")

    now = 1000.0

    # 4 неверных попытки — блокировки ещё нет
    for i in range(4):
        assert manager.verify_pin("000000", now=now) is False
        assert manager.is_locked_out(now=now) is False
        assert manager.get_failed_attempts() == i + 1

    # 5-я неверная попытка — активирует блокировку
    assert manager.verify_pin("000000", now=now) is False
    assert manager.is_locked_out(now=now) is True
    assert manager.get_failed_attempts() == 5
    assert manager.get_remaining_lockout_seconds(now=now) == 900.0

    # 6-я попытка даже с верным PIN отклоняется из-за блокировки
    assert manager.verify_pin("112233", now=now) is False

    # Спустя 901 секунду блокировка спадает
    now_after = now + 901.0
    assert manager.is_locked_out(now=now_after) is False
    assert manager.verify_pin("112233", now=now_after) is True
    assert manager.get_failed_attempts() == 0


def test_reset_lockout_manual(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file, max_attempts=5)
    manager.set_pin("1234")

    for _ in range(5):
        manager.verify_pin("wrong")

    assert manager.is_locked_out() is True
    manager.reset_lockout()
    assert manager.is_locked_out() is False
    assert manager.verify_pin("1234") is True


def test_elevate_session_and_ttl_expiry(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file, session_ttl=900.0)

    now = 5000.0
    channel = "telegram"
    user_id = 955111
    session_id = "sess-abc"

    assert manager.is_elevated(channel, user_id, session_id, now=now) is False

    manager.elevate_session(channel, user_id, session_id, now=now)
    assert manager.is_elevated(channel, user_id, session_id, now=now) is True

    # 899 секунд — всё ещё активна
    assert manager.is_elevated(channel, user_id, session_id, now=now + 899.0) is True

    # 901 секунда — истекла по TTL (15 минут)
    assert manager.is_elevated(channel, user_id, session_id, now=now + 901.0) is False


def test_session_isolation(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.elevate_session("telegram", 955111, "sess-1")

    # Совпадение тройки
    assert manager.is_elevated("telegram", 955111, "sess-1") is True

    # Разные поля тройки должны возвращать False
    assert manager.is_elevated("telegram", 955111, "sess-2") is False  # другой session_id
    assert manager.is_elevated("telegram", 888888, "sess-1") is False  # другой user_id
    assert manager.is_elevated("cli", 955111, "sess-1") is False       # другой channel


def test_verify_and_elevate_integration(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.set_pin("5555")

    now = 100.0
    channel, user_id, session_id = "telegram", 955111, "sess-xyz"

    # Неверный PIN
    ok, msg = manager.verify_and_elevate(channel, user_id, session_id, "1234", now=now)
    assert ok is False
    assert "Неверный PIN-код" in msg
    assert manager.is_elevated(channel, user_id, session_id, now=now) is False

    # Верный PIN
    ok, msg = manager.verify_and_elevate(channel, user_id, session_id, "5555", now=now)
    assert ok is True
    assert manager.is_elevated(channel, user_id, session_id, now=now) is True


def test_telegram_owner_verification(
    tmp_pin_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_TELEGRAM_OWNER_ID", "955111")
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)

    assert manager.is_telegram_owner(955111) is True
    assert manager.is_telegram_owner("955111") is True
    assert manager.is_telegram_owner(123456) is False

    # Без заданного ID — fail-closed
    monkeypatch.delenv("ANTIGONA_TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.delenv("ANTIGONA_OWNER_ID", raising=False)
    manager_unconfigured = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    assert manager_unconfigured.is_telegram_owner(955111) is False


def test_cli_owner_verification(
    tmp_pin_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTIGONA_OWNER_TOKEN", "secret-token-123")
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)

    # Проверка по токену
    assert manager.is_cli_owner(owner_token="secret-token-123") is True
    assert manager.is_cli_owner(owner_token="wrong-token") is False

    # Проверка текущего пользователя ОС: владелец CLI должен быть
    # сконфигурирован (ANTIGONA_CLI_OWNER_USER); передача текущего
    # username без конфигурации — fail-closed (False).
    import getpass

    current_user = getpass.getuser()
    assert manager.is_cli_owner(os_user=current_user) is False  # CLI owner unset
    monkeypatch.setenv("ANTIGONA_CLI_OWNER_USER", current_user)
    manager2 = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    assert manager2.is_cli_owner(os_user=current_user) is True
    assert manager2.is_cli_owner(os_user="invalid_nonexistent_user_xyz") is False


def test_lock_session_and_lock_all(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.elevate_session("telegram", 111, "s1")
    manager.elevate_session("telegram", 222, "s2")

    # Сброс конкретной сессии (/lock)
    assert manager.lock_session("telegram", 111, "s1") is True
    assert manager.is_elevated("telegram", 111, "s1") is False
    assert manager.is_elevated("telegram", 222, "s2") is True

    # Аварийный сброс всех сессий
    revoked = manager.lock_all()
    assert revoked == 1
    assert manager.is_elevated("telegram", 222, "s2") is False


def test_recovery_script_tty_check_refusal(tmp_pin_file: Path) -> None:
    # При force_tty_check=True в неинтерактивной среде (pytest runner sys.stdin.isatty() is False)
    exit_code = run_recovery_pin_reset(
        new_pin="777888", pin_file_path=tmp_pin_file, force_tty_check=True
    )
    assert exit_code == 1


def test_recovery_script_success_reset(tmp_pin_file: Path) -> None:
    manager = OwnerOverrideManager(pin_file_path=tmp_pin_file)
    manager.set_pin("1111")
    for _ in range(5):
        manager.verify_pin("wrong")
    assert manager.is_locked_out() is True

    # Запуск сброса без TTY проверки для теста
    exit_code = run_recovery_pin_reset(
        new_pin="9999", pin_file_path=tmp_pin_file, force_tty_check=False
    )
    assert exit_code == 0

    assert manager.is_locked_out() is False
    assert manager.verify_pin("9999") is True


@pytest.mark.skipif(sys.platform == "win32", reason='POSIX sh script cannot execute on Windows (WinError 193) (Wave 4)')
def test_recovery_pin_sh_refuses_non_tty(tmp_path: Path) -> None:
    sh_script = Path("src/antigona/bin/recovery_pin.sh").resolve()
    assert sh_script.exists()

    # Запуск recovery_pin.sh через stdin pipe (не TTY)
    res = subprocess.run(
        [str(sh_script)],
        input=b"",
        capture_output=True,
    )
    assert res.returncode == 1
    assert "ОШИБКА" in res.stderr.decode("utf-8")
