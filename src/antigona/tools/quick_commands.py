"""Quick Commands — shell-команды без вызова LLM."""

import logging
import shlex
import subprocess

from antigona.core.paths import home_dir

logger = logging.getLogger(__name__)

# The disk-usage quick command must report the *real* home directory, derived
# from the single home resolver (ADR-007). The literal "/root" used to be
# hardcoded, so on a host where ANTIGONA_HOME_DIR/HOME != /root the command
# showed a directory that does not belong to the current user. On this host
# home_dir() == "/root", so the rendered command is byte-identical.
_HOME = shlex.quote(str(home_dir()))

_QUICK_COMMANDS: dict[str, tuple[str, str]] = {
    "status": ("exec", "systemctl --user status hermes-gateway.service 2>&1 | head -20"),
    "gpu": ("exec", "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>&1 || echo 'No NVIDIA GPU found'"),
    "uptime": ("exec", "uptime -p"),
    "df": ("exec", f"df -h / {_HOME} 2>&1"),
    "ps": ("exec", "ps aux --sort=-%mem | head -10"),
    "memory": ("exec", "free -h"),
    "disk": ("exec", "df -h / 2>&1"),
    "whoami": ("exec", "whoami"),
    "date": ("exec", "date '+%Y-%m-%d %H:%M:%S'"),
}


def get_quick_command(name: str) -> tuple[str, str] | None:
    """Get a quick command by name. Returns (type, command) or None."""
    return _QUICK_COMMANDS.get(name.lower())


def list_quick_commands() -> dict[str, str]:
    """List all available quick commands → description."""
    return {
        "status": "Статус gateway",
        "gpu": "Загрузка GPU",
        "uptime": "Время работы системы",
        "df": "Использование диска",
        "ps": "Топ процессов по памяти",
        "memory": "Использование RAM",
        "disk": "Свободное место на /",
        "whoami": "Текущий пользователь",
        "date": "Текущее время",
    }


def execute_quick_command(name: str) -> str:
    """Execute a quick command and return output."""
    cmd_info = get_quick_command(name)
    if cmd_info is None:
        return f"❌ Неизвестная команда: /{name}"

    cmd_type, command = cmd_info
    if cmd_type == "exec":
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = result.stdout or result.stderr or "(no output)"
            return f"`/{name}`\n```\n{output.strip()[:3000]}\n```"
        except subprocess.TimeoutExpired:
            return f"❌ Команда /{name} превысила таймаут (15s)"
        except Exception as e:
            return f"❌ Ошибка /{name}: {e}"

    return f"❌ Неизвестный тип команды: {cmd_type}"
