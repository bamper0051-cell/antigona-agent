"""RiskClassifier — классификация рисков действий Antigona.

Определяет уровень риска для каждого действия LLM на основе
типа действия, пути и содержимого.

Уровни риска (§14 мастер-промпта):
    LOW:      чтение статуса, поиск, анализ файлов
    MEDIUM:   установка пакетов, изменение некритичной конфигурации, создание файлов
    HIGH:     остановка сервисов, изменение firewall, работа с секретами, удаление файлов
    CRITICAL: массовое удаление, удаление БД, очистка сервера, экспорт ключей
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from antigona.core.paths import home_dir


class RiskLevel(StrEnum):
    """Уровень риска действия.

    Используется для определения необходимого уровня аутентификации.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ── Константы для классификации ────────────────────────────────────────────

# Пути, запись в которые считается HIGH-RISK.
# Первый элемент — домашний каталог, выводится из единственного резолвера
# ``antigona.core.paths.home_dir()`` (уважает ANTIGONA_HOME_DIR), а не
# хардкодится: иначе классификатор не считает реальный home высокорисковым,
# когда HOME/ANTIGONA_HOME_DIR != /root (единый источник истины, ADR-007).
HIGH_RISK_PATHS: list[str] = [
    str(home_dir()),
    "/etc",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var",
    "/boot",
    "/opt",
    "/lib",
    "/lib64",
    "/home",
    "/sys",
    "/proc",
    "/dev",
    # Windows-системные корни: запись сюда — всегда HIGH (fail-closed).
    # Сравнение ниже casefold-нормализованное, поэтому регистр не важен.
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\users\\public",
    "c:\\system volume information",
    "c:\\recovery",
]

# Расширения и имена файлов / директорий, запись в которые считается SENSITIVE / HIGH-RISK
SENSITIVE_WRITE_EXTENSIONS: frozenset[str] = frozenset({
    ".env",
    ".envrc",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cert",
    ".jks",
    ".keystore",
    ".secret",
    ".token",
    ".password",
    ".pwd",
    ".credentials",
    ".ovpn",
    ".kubeconfig",
})

SENSITIVE_WRITE_FILENAMES: frozenset[str] = frozenset({
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "authorized_keys",
    "known_hosts",
})

SENSITIVE_DIRECTORY_NAMES: frozenset[str] = frozenset({
    ".ssh",
    ".gnupg",
    ".vault",
    ".secrets",
    ".security",
    ".git",
})

# Reads can disclose credentials without mutating the filesystem.  The read
# policy is intentionally stricter than the write policy: AGENTS.md requires
# explicit owner authorization for JSON as well as key/env material.
SENSITIVE_READ_EXTENSIONS: frozenset[str] = SENSITIVE_WRITE_EXTENSIONS | frozenset(
    {".json"}
)

SENSITIVE_READ_FILENAMES: frozenset[str] = SENSITIVE_WRITE_FILENAMES | frozenset(
    {
        ".netrc",
        ".pgpass",
        "credentials",
        "credentials.json",
        "owner_pin.json",
    }
)

SENSITIVE_READ_DIRECTORY_NAMES: frozenset[str] = SENSITIVE_DIRECTORY_NAMES | frozenset(
    {
        ".aws",
        ".azure",
        ".kube",
        "credentials",
        "keys",
        "secrets",
        "vault",
    }
)

SENSITIVE_READ_NAME_FRAGMENTS: tuple[str, ...] = (
    "api_key",
    "credential",
    "owner_pin",
    "password",
    "private_key",
    "secret",
)

WRITE_ACTION_TYPES: frozenset[str] = frozenset({
    "WRITE_TEXT",
    "WRITE_FILE",
    "CREATE_FILE",
    "APPEND_FILE",
    "EDIT",
    "EDIT_FILE",
    "UPDATE_FILE",
    "PATCH",
    "PATCH_FILE",
    "WORKSPACE_WRITE",
    "WORKSPACE_WRITE_TEXT",
    "FILE_WRITE",
    "FILESYSTEM_WRITE",
})

# Ключевые слова для CRITICAL-RISK в shell-командах
CRITICAL_SHELL_KEYWORDS: list[str] = [
    "rm -rf",
    "rm -fr",
    "rm -r -f",
    "rm --recursive",
    "mkfs",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "fdisk",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    "halt",
    "poweroff",
    "> /dev/sda",
    "> /dev/nvme",
    "format",
    "diskpart",
    "pvcreate",
    "vgremove",
    "lvremove",
    "mdadm --zero-superblock",
    "wipefs",
    "parted",
    "drop database",
    "dropdb",
    "truncate table",
    "systemctl stop",
    "systemctl restart",
    "systemctl disable",
    "service stop",
    "service restart",
    "iptables",
    "nftables",
    "ufw",
    "firewall-cmd",
    "kill -9",
    "pkill -9",
    "pkill -f",
    "killall",
]

# Ключевые слова для HIGH-RISK в shell-командах
HIGH_SHELL_KEYWORDS: list[str] = [
    "rm ",
    "rm -",
    "rmdir ",
    "del ",
    "wipe ",
    "shred ",
    "iptables",
    "nftables",
    "ufw ",
    "passwd",
    "chmod 777",
    "chown ",
    "usermod",
    "groupmod",
    "systemctl stop",
    "systemctl disable",
    "kill -9",
    "pkill -9",
    "service ",
]

# Ключевые слова для MEDIUM-RISK в shell-командах
MEDIUM_SHELL_KEYWORDS: list[str] = [
    "apt ",
    "apt-get ",
    "pip ",
    "pip3 ",
    "npm ",
    "git clone",
    "git pull",
    "git fetch",
    "wget ",
    "curl ",
    "docker pull",
    "docker run",
    "podman pull",
    "snap install",
    "flatpak install",
    "brew install",
]

# Расширения файлов, отправка которых считается HIGH-RISK
HIGH_RISK_FILE_EXTENSIONS: set[str] = {
    ".json",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cert",
    ".jks",
    ".keystore",
    ".env",
    ".envrc",
    ".ini",
    ".cfg",
    ".config",
    ".yaml",
    ".yml",
    ".toml",
    ".kubeconfig",
    ".kube",
    ".ovpn",
    ".rdp",
    ".ssh",
    ".pgpass",
    ".my.cnf",
    ".netrc",
    ".npmrc",
    ".dockercfg",
    ".docker",
    ".kaggle",
    ".credentials",
    ".secret",
    ".token",
    ".password",
    ".pwd",
    ".backup",
    ".sql",
    ".db",
    ".sqlite",
    ".dump",
    ".tar.gz",
    ".zip",
    ".7z",
}

# Ключевые слова для HIGH-RISK в RUN_CODE (import)
HIGH_CODE_IMPORTS: list[str] = [
    "import os",
    "import subprocess",
    "import shutil",
    "from os",
    "from subprocess",
    "import ctypes",
    "import fcntl",
    "import ptrace",
    "import signal",
    "os.",
    "subprocess.",
    "shutil.",
    "__import__('os'",
    "__import__('subprocess'",
]


# ── Классификатор ──────────────────────────────────────────────────────────


def _truncate_desc(text: str, max_len: int = 80) -> str:
    """Обрезать описание до readable длины с многоточием."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def resolve_workspace_root(workspace_root: str | Path | None = None) -> Path | None:
    """Получить канонический корень workspace через Unified Paths API или параметр."""
    if workspace_root is not None:
        try:
            return Path(workspace_root).resolve()
        except Exception:
            return None
    try:
        from antigona.core import paths

        return paths.workspace_dir().resolve()
    except Exception:
        return None


def is_write_action(action_type: str) -> bool:
    """Проверить, относится ли действие к записи/модификации файлов."""
    norm = action_type.upper().strip().replace(".", "_").replace(":", "_").replace("-", "_")
    if norm in WRITE_ACTION_TYPES:
        return True
    if norm.startswith("WRITE_") or norm.endswith("_WRITE") or norm.endswith("_WRITE_TEXT"):
        return True
    return False


def is_safe_workspace_path(path: str, workspace_root: str | Path | None = None) -> bool:
    """Проверить, что путь безопасно разрешается строго внутри workspace."""
    resolved = resolve_confined_workspace_path(path, workspace_root=workspace_root)
    if resolved is None:
        return False

    ws_root = resolve_workspace_root(workspace_root)
    if ws_root is None or resolved == ws_root:
        return False

    try:
        rel = resolved.relative_to(ws_root)
    except ValueError:
        return False

    for part in rel.parts:
        part_lower = part.lower()
        if part_lower in SENSITIVE_DIRECTORY_NAMES:
            return False
        if part_lower.startswith(".env"):
            return False
        if part_lower in SENSITIVE_WRITE_FILENAMES:
            return False

    filename_lower = resolved.name.lower()
    return not any(filename_lower.endswith(ext) for ext in SENSITIVE_WRITE_EXTENSIONS)


def resolve_confined_workspace_path(
    path: str, workspace_root: str | Path | None = None
) -> Path | None:
    """Resolve *path* inside the configured workspace, or fail closed.

    This helper checks confinement only.  Sensitivity is a separate decision:
    a ``.json`` file may be inside the workspace but still require a grant.
    Symlink escapes are rejected because ``Path.resolve`` is compared to the
    canonical workspace root.
    """
    if not path or not path.strip():
        return None

    # Недопустимые символы.
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return None
    if "%" in path:
        return None
    # Backslash разрешён ТОЛЬКО как разделитель валидного Windows-абсолютного
    # пути (C:\\Users\\...). Любой другой backslash (\\root\\..., Linux-нотация,
    # escape-попытка, ..\\..\\etc) — fail-closed, как исторически.
    is_win_abs = bool(re.match(r"^[A-Za-z]:[\\/]", path))
    if "\\" in path and not is_win_abs:
        return None

    raw_path = Path(path)
    if any(part in {"..", ""} for part in raw_path.parts):
        return None

    ws_root = resolve_workspace_root(workspace_root)
    if ws_root is None:
        return None

    try:
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        else:
            resolved = (ws_root / raw_path).resolve()
    except (ValueError, OSError, RuntimeError):
        return None

    try:
        resolved.relative_to(ws_root)
    except ValueError:
        return None
    return resolved


def read_target_escapes_workspace(path: str, workspace_root: str | Path | None = None) -> bool:
    """Return whether a *relative* read target escapes the workspace fence.

    The read handler resolves a bare relative path against ``project_root()``
    when that file exists (``project_root()`` is the parent of the workspace
    dir in the deployed layout), so ``resolve_confined_workspace_path`` — which
    resolves relatives against the workspace root — is not enough on its own.
    This mirrors the handler's resolution and reports an escape so the caller
    can classify the read ``HIGH``.  A path that does not exist under
    ``project_root()`` cannot disclose anything (the handler returns
    ``NOT_FOUND``), so it is not treated as an escape.
    """
    if not path or not path.strip():
        return False
    raw = Path(path)
    if raw.is_absolute():
        return False  # absolute paths are handled by resolve_confined_workspace_path
    ws_root = resolve_workspace_root(workspace_root)
    if ws_root is None:
        return True
    try:
        from antigona.core import paths

        alt = (paths.project_root() / raw).resolve()
    except Exception:
        return True  # fail closed, like resolve_confined_workspace_path
    if not alt.exists():
        return False
    try:
        alt.relative_to(ws_root)
    except ValueError:
        return True
    return False


def is_sensitive_read_target(path: str) -> bool:
    """Return whether a read target can contain credentials or key material."""
    if not path or not path.strip():
        return True
    normalized = path.replace("\\", "/")
    parts = [part.casefold() for part in normalized.split("/") if part]
    if not parts:
        return True
    filename = parts[-1]
    if any(part in SENSITIVE_READ_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if filename in SENSITIVE_READ_FILENAMES or filename.startswith(".env"):
        return True
    if any(filename.endswith(ext) for ext in SENSITIVE_READ_EXTENSIONS):
        return True
    return any(fragment in filename for fragment in SENSITIVE_READ_NAME_FRAGMENTS)


def is_sensitive_write_target(path: str) -> bool:
    """Проверить, указывает ли путь на секретный/чувствительный файл."""
    if not path:
        return False
    lower = path.lower()
    if any(s in lower for s in (".env", ".ssh", ".secrets", ".vault", ".security")):
        return True
    filename = Path(path).name.lower()
    if filename in SENSITIVE_WRITE_FILENAMES:
        return True
    if any(filename.endswith(ext) for ext in SENSITIVE_WRITE_EXTENSIONS):
        return True
    return False


def _normalize_path(path: str) -> str:
    """Нормализовать путь для сравнения."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except (ValueError, OSError, RuntimeError):
        return path


def is_high_risk_path(path: str, workspace_root: str | Path | None = None) -> bool:
    """Проверить, является ли путь системным/критичным или чувствительным."""
    if not path or not path.strip():
        return False
    if is_safe_workspace_path(path, workspace_root=workspace_root):
        return False

    # Недопустимые символы. Backslash — только валидный Windows-абсолютный
    # разделитель; иначе fail-closed (escape/traversal/не-Windows-нотация).
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or "%" in path:
        return True
    is_win_abs = bool(re.match(r"^[A-Za-z]:[\\/]", path))
    if "\\" in path and not is_win_abs:
        return True

    raw_path = Path(path)
    if any(part == ".." for part in raw_path.parts) or ".." in path:
        return True

    if is_sensitive_write_target(path):
        return True

    norm = _normalize_path(path)
    for high_path in HIGH_RISK_PATHS:
        if path.casefold().startswith(high_path.casefold()) or norm.casefold().startswith(high_path.casefold()):
            return True

    ws_root = resolve_workspace_root(workspace_root)
    try:
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        elif ws_root is not None:
            resolved = (ws_root / raw_path).resolve()
        else:
            resolved = raw_path.resolve()
    except (ValueError, OSError, RuntimeError):
        return True

    resolved_str = str(resolved)
    resolved_cf = resolved_str.casefold()
    for high_path in HIGH_RISK_PATHS:
        hp = high_path.casefold()
        if resolved_cf == hp or resolved_cf.startswith(hp + "/") or resolved_cf.startswith(hp + "\\") or resolved_cf.startswith(hp):
            return True

    if is_sensitive_write_target(resolved_str):
        return True

    for part in resolved.parts:
        part_lower = part.lower()
        if (
            part_lower in SENSITIVE_DIRECTORY_NAMES
            or part_lower.startswith(".env")
            or part_lower in SENSITIVE_WRITE_FILENAMES
        ):
            return True

    # An executable/script file written outside the workspace is a HIGH-risk
    # escape target: a model could plant a runnable artifact on the host without
    # approval.  This intentionally does NOT classify every out-of-workspace
    # write as HIGH (e.g. /tmp/test.txt stays MEDIUM); only runnable artifacts.
    if not is_safe_workspace_path(path, workspace_root=workspace_root):
        script_ext = {".sh", ".bash", ".py", ".pl", ".rb", ".php", ".exe", ".bat", ".cmd", ".ps1"}
        if Path(resolved).name.lower().endswith(tuple(script_ext)):
            return True

    return False


class RiskClassifier:
    """Классифицирует действия по уровню риска.

    Анализирует тип действия (ActionType), путь к файлу и содержимое
    для определения уровня риска по 4-уровневой шкале.

    Использование::

        classifier = RiskClassifier()

        level = classifier.classify("WRITE_FILE", path="/etc/hosts")
        # → RiskLevel.HIGH

        level = classifier.classify("RUN_SHELL", command="apt install python3")
        # → RiskLevel.MEDIUM

        if classifier.requires_auth(level):
            print("Требуется подтверждение!")
    """

    def __init__(self) -> None:
        pass

    def classify(
        self,
        action_type: str,
        path: str = "",
        content: str = "",
    ) -> RiskLevel:
        """Определить уровень риска действия.

        Args:
            action_type: Тип действия (ActionType.value).
            path: Путь к файлу (для WRITE_FILE, SEND_FILE, READ_FILE).
            content: Содержимое/команда/код (для RUN_SHELL, RUN_CODE).

        Returns:
            RiskLevel: уровень риска.
        """
        action = action_type.upper().strip()
        normalized_path = self._normalize_path(path)

        if action in ("READ_FILE", "READ_TEXT", "WORKSPACE_READ_TEXT", "WORKSPACE.READ_TEXT", "FILE_READ", "FILESYSTEM_READ"):
            return self._classify_read_file(path)
        elif action == "SEARCH_FILES":
            return RiskLevel.LOW
        elif self._is_write_action(action_type):
            return self._classify_write_file(path)
        elif action == "RUN_SHELL":
            return self._classify_run_shell(content)
        elif action == "TMUX":
            # tmux start/send are host-shell execution surfaces.  The tool is
            # gated as a whole so alternate actions cannot bypass approval.
            return RiskLevel.HIGH
        elif action == "RUN_CODE":
            return self._classify_run_code(content)
        elif action == "CONFIGURE_KEY":
            return RiskLevel.HIGH
        elif action == "SEND_FILE":
            return self._classify_send_file(normalized_path, content)
        elif action == "GENERATE_IMAGE":
            return RiskLevel.LOW
        elif action == "MEMORIZE":
            return RiskLevel.LOW
        elif action == "WEB_SEARCH":
            return RiskLevel.LOW
        else:
            # Неизвестные действия — MEDIUM по умолчанию
            return RiskLevel.MEDIUM

    # ── Классификация по типам ───────────────────────────────────────────

    def _classify_read_file(self, path: str) -> RiskLevel:
        """Only ordinary workspace reads are LOW; secrets/escapes are HIGH.

        A missing/blank target is a degenerate call with nothing to disclose
        (the read handler requires ``path`` and rejects it), so it stays LOW
        rather than being force-escalated.
        """
        if not path or not path.strip():
            return RiskLevel.LOW
        if resolve_confined_workspace_path(path) is None:
            return RiskLevel.HIGH
        if is_sensitive_read_target(path):
            return RiskLevel.HIGH
        # The read handler falls back to ``project_root()`` for a relative path
        # that exists there; a path that leaves the workspace that way is HIGH.
        if read_target_escapes_workspace(path):
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def _classify_write_file(self, path: str) -> RiskLevel:
        """Запись внутри workspace — LOW (AUTO), в системные/критичные — HIGH, иначе MEDIUM."""
        if self._is_safe_workspace_write(path):
            return RiskLevel.LOW
        if self._is_high_risk_path(path) or self._is_sensitive_write_target(path):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    def _classify_run_shell(self, command: str) -> RiskLevel:
        """Классификация shell-команд."""
        if not command:
            return RiskLevel.MEDIUM

        cmd_lower = command.lower().strip()

        # CRITICAL: деструктивные команды
        if self._matches_any(cmd_lower, CRITICAL_SHELL_KEYWORDS):
            return RiskLevel.CRITICAL

        # HIGH: удаление, изменение безопасности
        if self._matches_any(cmd_lower, HIGH_SHELL_KEYWORDS):
            return RiskLevel.HIGH

        # MEDIUM: установка пакетов, работа с репозиториями
        if self._matches_any(cmd_lower, MEDIUM_SHELL_KEYWORDS):
            return RiskLevel.MEDIUM

        # Если команда не содержит опасных ключевых слов — MEDIUM
        # (любая shell-команда потенциально опасна)
        return RiskLevel.MEDIUM

    def _classify_run_code(self, code: str) -> RiskLevel:
        """Классификация выполняемого кода.

        Код с импортами системных модулей — HIGH, иначе MEDIUM.
        """
        if not code:
            return RiskLevel.MEDIUM

        code_lower = code.lower()
        if self._matches_any(code_lower, HIGH_CODE_IMPORTS):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    def _classify_send_file(
        self,
        path: str,
        content: str,  # noqa: ARG002
    ) -> RiskLevel:
        """Отправка файлов с секретами — HIGH, иначе MEDIUM."""
        if self._is_high_risk_extension(path):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    # ── Вспомогательные методы ──────────────────────────────────────────

    @staticmethod
    def _is_write_action(action_type: str) -> bool:
        """Проверить, относится ли действие к записи/модификации файлов."""
        return is_write_action(action_type)

    @staticmethod
    def _get_workspace_root() -> Path | None:
        """Получить канонический корень workspace через Unified Paths API."""
        return resolve_workspace_root()

    def _is_safe_workspace_write(self, path: str) -> bool:
        """Проверить, что путь безопасно разрешается строго внутри workspace."""
        return is_safe_workspace_path(path)

    def _is_sensitive_write_target(self, path: str) -> bool:
        """Проверить, указывает ли путь на секретный/чувствительный файл."""
        return is_sensitive_write_target(path)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Нормализовать путь для сравнения."""
        return _normalize_path(path)

    def _is_high_risk_path(self, path: str) -> bool:
        """Проверить, является ли путь системным/критичным."""
        return is_high_risk_path(path)

    @staticmethod
    def _is_high_risk_extension(path: str) -> bool:
        """Проверить расширение файла на риск."""
        if not path:
            return False
        lower_path = path.lower().strip()
        for ext in HIGH_RISK_FILE_EXTENSIONS:
            if lower_path.endswith(ext):
                return True
        return False

    @staticmethod
    def _matches_any(text: str, keywords: list[str]) -> bool:
        """Проверить, содержит ли текст любое из ключевых слов."""
        for kw in keywords:
            if kw in text:
                return True
        return False

    # ── Аутентификация ────────────────────────────────────────────────────

    @staticmethod
    def requires_auth(level: RiskLevel) -> bool:
        """Требует ли действие аутентификации?

        Args:
            level: Уровень риска.

        Returns:
            True если нужно подтверждение владельца/PIN.
        """
        return level != RiskLevel.LOW

    @staticmethod
    def requires_otp(level: RiskLevel) -> bool:
        """Требует ли действие OTP-подтверждения?

        Args:
            level: Уровень риска.

        Returns:
            True если нужен одноразовый код/PIN.
        """
        return level in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    @staticmethod
    def requires_totp(level: RiskLevel) -> bool:
        """Требует ли действие TOTP-подтверждения?

        Args:
            level: Уровень риска.

        Returns:
            True если нужен TOTP (только CRITICAL).
        """
        return level == RiskLevel.CRITICAL

    @staticmethod
    def requirement_summary(level: RiskLevel) -> str:
        """Человекочитаемое описание необходимой аутентификации.

        Args:
            level: Уровень риска.

        Returns:
            Строка с описанием требований.
        """
        summaries = {
            RiskLevel.LOW: (
                "✅ Без подтверждения — действие безопасно"
            ),
            RiskLevel.MEDIUM: (
                "🔑 Требуется подтверждение владельца"
            ),
            RiskLevel.HIGH: (
                "🔐 Требуется PIN-код (OTP)"
            ),
            RiskLevel.CRITICAL: (
                "🚨 Требуется PIN-код + TOTP (двухфакторная аутентификация)"
            ),
        }
        return summaries.get(level, "❓ Неизвестный уровень риска")

    @staticmethod
    def action_description(
        action_type: str,
        path: str = "",
        content: str = "",
    ) -> str:
        """Человекочитаемое описание действия для OTP-челленджа.

        Генерирует краткое описание, понятное владельцу,
        для отображения в OTP-сообщении.

        Args:
            action_type: Тип действия (ActionType).
            path: Путь к файлу (для WRITE_FILE, SEND_FILE, READ_FILE).
            content: Команда/код/содержимое (для RUN_SHELL, RUN_CODE).

        Returns:
            Человекочитаемое описание действия.
        """
        action_upper = action_type.upper().strip()

        descriptions = {
            "WRITE_FILE": f"Запись в файл: {path}" if path else "Запись файла",
            "WRITE_TEXT": f"Запись в файл: {path}" if path else "Запись файла",
            "CREATE_FILE": f"Создание файла: {path}" if path else "Создание файла",
            "APPEND_FILE": f"Добавление в файл: {path}" if path else "Добавление в файл",
            "EDIT": f"Редактирование файла: {path}" if path else "Редактирование файла",
            "EDIT_FILE": f"Редактирование файла: {path}" if path else "Редактирование файла",
            "UPDATE_FILE": f"Обновление файла: {path}" if path else "Обновление файла",
            "SEND_FILE": f"Отправка файла: {path}" if path else "Отправка файла",
            "RUN_SHELL": _truncate_desc(f"Выполнение команды: {content}", 80)
            if content
            else "Выполнение shell-команды",
            "RUN_CODE": _truncate_desc(f"Выполнение кода: {content}", 80)
            if content
            else "Выполнение произвольного кода",
            "CONFIGURE_KEY": f"Изменение настройки: {path}" if path else "Изменение конфигурации",
            "READ_FILE": f"Чтение файла: {path}" if path else "Чтение файла",
            "READ_TEXT": f"Чтение файла: {path}" if path else "Чтение файла",
            "SEARCH_FILES": f"Поиск: {path}" if path else "Поиск файлов",
            "GENERATE_IMAGE": "Генерация изображения",
            "MEMORIZE": "Сохранение в память",
            "WEB_SEARCH": "Поиск в интернете",
        }

        return descriptions.get(action_upper, f"Действие: {action_type} ({path or content})")
