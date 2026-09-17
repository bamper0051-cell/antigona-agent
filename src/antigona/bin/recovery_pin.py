"""Recovery PIN Script — локальный аварийный сброс PIN-кода Antigona.

РАБОТАЕТ ТОЛЬКО ИЗ ЛОКАЛЬНОЙ КОНСОЛИ (TTY CHECK).
Недоступен удалённо / из веб-запросов / через неинтерактивный stdin.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from antigona.security.owner_override import OwnerOverrideManager


def check_tty() -> bool:
    """Проверить, подключен ли текущий ввод к локальной интерактивной консоли (TTY)."""
    return sys.stdin.isatty()


def run_recovery_pin_reset(
    new_pin: str | None = None,
    pin_file_path: Path | str | None = None,
    force_tty_check: bool = True,
) -> int:
    """Выполнить сброс PIN-кода из локальной TTY-консоли.

    Args:
        new_pin: Заданный новый PIN (для программного вызова при TTY check) или None для getpass.
        pin_file_path: Кастомный путь к файлу PIN-хеша.
        force_tty_check: Если True, обязательно проверяет sys.stdin.isatty().

    Returns:
        0 при успехе, 1 при ошибке.
    """
    if force_tty_check and not check_tty():
        print(
            "ОШИБКА: recovery_pin должен запускаться ИСКЛЮЧИТЕЛЬНО из локальной TTY-консоли сервера.",
            file=sys.stderr,
        )
        return 1

    if new_pin is None:
        try:
            print("=== Сброс PIN-кода владельца Antigona (Локальный TTY) ===")
            pin_input = getpass.getpass("Введите новый PIN-код: ")
            confirm_input = getpass.getpass("Подтвердите новый PIN-код: ")
        except (KeyboardInterrupt, EOFError):
            print("\nОперация отменена.", file=sys.stderr)
            return 1

        if not pin_input:
            print("ОШИБКА: PIN-код не может быть пустым.", file=sys.stderr)
            return 1

        if pin_input != confirm_input:
            print("ОШИБКА: Введённые PIN-коды не совпадают.", file=sys.stderr)
            return 1
        target_pin = pin_input
    else:
        target_pin = new_pin

    manager = OwnerOverrideManager(pin_file_path=pin_file_path)
    try:
        manager.set_pin(target_pin)
        manager.reset_lockout()
        revoked_sessions = manager.lock_all()
        print(
            f"УСПЕХ: PIN-код успешно сброшен. Блокировка попыток отменена. Отозвано сессий: {revoked_sessions}."
        )
        return 0
    except Exception as e:
        print(f"ОШИБКА при сбросе PIN-кода: {e}", file=sys.stderr)
        return 1


def main() -> None:
    """CLI Точка входа для python -m antigona.bin.recovery_pin."""
    sys.exit(run_recovery_pin_reset())


if __name__ == "__main__":
    main()
