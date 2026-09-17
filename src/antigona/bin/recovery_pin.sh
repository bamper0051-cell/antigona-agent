#!/usr/bin/env bash
set -euo pipefail

# ── ТРЕБОВАНИЕ БЕЗОПАСНОСТИ: Только локальная интерактивная TTY консоль ──────
if [ ! -t 0 ]; then
    echo "ОШИБКА: recovery_pin.sh должен запускаться ИСКЛЮЧИТЕЛЬНО из локальной TTY-консоли сервера." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

PYTHON_BIN="python3"
if [ -f "${PROJECT_ROOT}/.venv-new/bin/python" ]; then
    PYTHON_BIN="${PROJECT_ROOT}/.venv-new/bin/python"
fi

PYTHONPATH="${PROJECT_ROOT}/src:${PYTHONPATH:-}" exec "${PYTHON_BIN}" -m antigona.bin.recovery_pin "$@"
