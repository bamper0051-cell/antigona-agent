"""Credentials — чтение приватных credentials из файлов/окружения.

Вынесено из старого security.py для сохранения обратной совместимости.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_private_credential(path: str) -> str:
    """Прочитать credential из файла с проверкой прав доступа.

    Args:
        path: Путь к файлу.

    Returns:
        Содержимое файла (строка).

    Raises:
        PermissionError: Если файл не принадлежит текущему пользователю
                        или имеет неправильные права (не 0600).
        ValueError: Если файл пуст.
    """
    credential_path = Path(path)
    info = credential_path.stat()

    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise PermissionError(
            "credential file must be owned by service user"
        )

    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("credential file mode must be 0600")

    value = credential_path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("credential file is empty")

    return value


def verifier_credential() -> str:
    """Получить credential верификатора.

    Порядок:
    1. Файл из ANTIGONA_VERIFIER_CREDENTIAL_FILE
    2. Переменная окружения ANTIGONA_VERIFIER_CREDENTIAL

    Returns:
        Строка с credential.

    Raises:
        KeyError: Если credential не задан нигде.
    """
    path = os.getenv("ANTIGONA_VERIFIER_CREDENTIAL_FILE")
    if path:
        return read_private_credential(path)
    return os.environ["ANTIGONA_VERIFIER_CREDENTIAL"]
