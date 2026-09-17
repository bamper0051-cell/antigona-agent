#!/usr/bin/env bash
# ⚠️ LEGACY LAUNCHER — НЕ официальный способ запуска (ADR-003).
# Canonical launcher: run.sh (полный env + Runtime Validator hard-fail).
# Этот скрипт запускает ТОЛЬКО gateway с ЧАСТИЧНЫМ env (только .env).
# Отключён по умолчанию. Для осознанного изолированного запуска:
#   ANTIGONA_ALLOW_LEGACY_LAUNCHER=1 ./start_gateway.sh
if [ "${ANTIGONA_ALLOW_LEGACY_LAUNCHER:-0}" != "1" ]; then
    echo "⛔ start_gateway.sh — LEGACY launcher, отключён (ADR-003). Используйте run.sh." >&2
    echo "   Для осознанного изолированного запуска: ANTIGONA_ALLOW_LEGACY_LAUNCHER=1 $0" >&2
    exit 1
fi
set -a
source /var/lib/antigona/.env
set +a
cd /var/lib/antigona
exec .venv-new/bin/python -m antigona.gateway
