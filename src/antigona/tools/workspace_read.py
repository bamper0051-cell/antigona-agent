"""``workspace.read_text`` — чтение файла ВНУТРИ рабочего пространства.

LOOP4 / DEFECT 2. Раньше read-запрос («прочитай этот файл обратно и покажи
мне…») не имел инструмента в task-пути: намерение доезжало до generic-ветки
роутера, ``_extract_shell_command`` возвращал ``None`` и задача молча
деградировала в ``workspace.write_text`` — вместо чтения файл перезаписывался.

Здесь read становится явным инструментом task-контракта. Граница ровно та же,
что у записи (:func:`antigona.filesystem.validate_relative_path`):

* путь — ТОЛЬКО workspace-relative; абсолютный путь (``/etc/passwd``) отвергается;
* ``..`` и выход за пределы workspace отвергаются;
* symlink-компонент в пути отвергается.

Поведение fail-closed: при любом нарушении границы возвращается
``ReadTextResult(ok=False, content="")`` — файл НЕ читается, содержимое наружу
не попадает.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from antigona.filesystem import WorkspaceViolation, validate_relative_path

#: Имя инструмента в task-контракте (``TaskCreate.tool_name``).
TOOL_NAME = "workspace.read_text"

#: Верхняя граница размера читаемого файла (защита от выгрузки гигабайта в чат).
MAX_READ_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ReadTextResult:
    """Результат чтения.

    Attributes:
        ok: Успех. ``False`` — файл НЕ прочитан (граница/отсутствие/размер).
        path: Полный путь прочитанного файла (пусто при отказе).
        content: Содержимое файла (пусто при отказе).
        sha256: Хеш прочитанных байтов.
        error: Причина отказа.
        tool_name: Имя инструмента, выполнившего чтение.
    """

    ok: bool
    path: str = ""
    content: str = ""
    sha256: str = ""
    error: str = ""
    tool_name: str = TOOL_NAME


def default_workspace_root() -> Path:
    """Корень рабочего пространства (тот же контракт, что у config.Settings)."""
    return Path(os.getenv("ANTIGONA_WORKSPACE", "./workspace")).resolve()


class WorkspaceReadTextTool:
    """Read-инструмент task-пути: UTF-8 текст строго внутри workspace."""

    name = TOOL_NAME
    description = "Read UTF-8 text file inside the workspace boundary"
    risk_level = "low"
    timeout_seconds = 10
    requires_approval = False
    sandbox_required = False

    def __init__(
        self,
        workspace: str | Path | None = None,
        max_bytes: int = MAX_READ_BYTES,
    ) -> None:
        self.workspace = (
            Path(workspace).resolve() if workspace else default_workspace_root()
        )
        self.max_bytes = max_bytes

    def execute(self, path: str) -> ReadTextResult:
        """Прочитать ``path`` (workspace-relative) и вернуть содержимое.

        Args:
            path: Путь относительно workspace. Абсолютный путь или выход за
                границу — отказ, а не чтение.
        """
        relative = (path or "").strip().strip("\"'`")
        if not relative:
            return ReadTextResult(False, error="empty path")

        try:
            validate_relative_path(self.workspace, relative)
        except WorkspaceViolation as exc:
            return ReadTextResult(False, error=f"path outside workspace: {exc}")
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            return ReadTextResult(False, error=f"unsafe path: {exc}")

        candidate = self.workspace / relative
        if candidate.is_symlink():
            return ReadTextResult(False, error="symlink target forbidden")

        target = candidate.resolve(strict=False)
        try:
            target.relative_to(self.workspace)
        except ValueError:
            return ReadTextResult(False, error="path outside workspace")

        if not target.is_file():
            return ReadTextResult(False, path=str(target), error="not a file")
        try:
            if target.stat().st_size > self.max_bytes:
                return ReadTextResult(
                    False, path=str(target), error=f"file too large (>{self.max_bytes} bytes)"
                )
            data = target.read_bytes()
        except OSError as exc:
            return ReadTextResult(False, path=str(target), error=str(exc))

        return ReadTextResult(
            True,
            path=str(target),
            content=data.decode("utf-8", errors="replace"),
            sha256=hashlib.sha256(data).hexdigest(),
        )


__all__ = [
    "MAX_READ_BYTES",
    "TOOL_NAME",
    "ReadTextResult",
    "WorkspaceReadTextTool",
    "default_workspace_root",
]
