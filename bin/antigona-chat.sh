#!/usr/bin/env bash
# ⚠️ DORMANT launcher — обёртка antigona-cli (LEGACY проект, ADR-008).
# Отключён по умолчанию. Для осознанного запуска:
#   ANTIGONA_ALLOW_LEGACY_LAUNCHER=1 bin/antigona-chat.sh
if [ "${ANTIGONA_ALLOW_LEGACY_LAUNCHER:-0}" != "1" ]; then
    echo "⛔ antigona-chat.sh — DORMANT launcher (antigona-cli), отключён (ADR-008)." >&2
    echo "   Для осознанного запуска: ANTIGONA_ALLOW_LEGACY_LAUNCHER=1 $0" >&2
    exit 1
fi
# Antigona CLI Interactive Chat — обёртка для antigona-cli
set -euo pipefail

# Derived from ${HOME} with an explicit override so the dormant launcher is not
# bound to a specific server user (docs: set ANTIGONA_LEGACY_CLI_DIR to relocate).
CLI_DIR="${ANTIGONA_LEGACY_CLI_DIR:-${HOME}/antigona-cli}"
CLI_EXEC="${CLI_DIR}/.venv/bin/antigona-cli"

if [ ! -f "$CLI_EXEC" ]; then
    echo "❌ antigona-cli not installed. Run setup first:" >&2
    echo "   cd ${CLI_DIR} && uv sync --all-extras" >&2
    exit 1
fi

exec "$CLI_EXEC" chat "$@"
