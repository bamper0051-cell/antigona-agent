#!/usr/bin/env bash
# systemd service wrapper for Antigona stack (canonical launcher, 2026-08-15)
# Replicates the env contract of run.sh WITHOUT pkill/validator/cleanup logic —
# systemd itself is the supervisor (Restart=on-failure, KillMode=mixed).
# Usage: service_wrapper.sh <gateway|verifier|worker|delivery|bot|orchestration>
set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$ROOT" || exit 1

# ── Load .env (regex-filtered, same as run.sh) ────────────────────
# Default is derived from ${HOME} (never a hardcoded server user); the live
# units pass EnvironmentFile= explicitly, so this is only a fallback. Override
# with ANTIGONA_ENV_FILE to point at another env file.
ENV_FILE="${ANTIGONA_ENV_FILE:-${HOME}/antigona.env}"
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && export "$line"
    done < "$ENV_FILE"
fi

# ── Environment defaults (same as run.sh) ──────────────────────────
export ANTIGONA_GATEWAY_HOST="${ANTIGONA_GATEWAY_HOST:-127.0.0.1}"
export ANTIGONA_GATEWAY_PORT="${ANTIGONA_GATEWAY_PORT:-8090}"
export ANTIGONA_VERIFIER_HOST="${ANTIGONA_VERIFIER_HOST:-127.0.0.1}"
export ANTIGONA_VERIFIER_PORT="${ANTIGONA_VERIFIER_PORT:-8091}"
export ANTIGONA_DATABASE_URL="${ANTIGONA_DATABASE_URL:-sqlite:///./antigona.db}"
export ANTIGONA_WORKSPACE="${ANTIGONA_WORKSPACE:-./workspace}"
export ANTIGONA_VERIFIER_URL="http://${ANTIGONA_VERIFIER_HOST}:${ANTIGONA_VERIFIER_PORT}"
export ANTIGONA_GATEWAY_URL="http://${ANTIGONA_GATEWAY_HOST}:${ANTIGONA_GATEWAY_PORT}"
export ANTIGONA_GATEWAY_TOKEN="${ANTIGONA_GATEWAY_TOKEN:-gateway-token}"
export PYTHONUNBUFFERED=1
export NO_PROXY="*"
export no_proxy="*"
# PHASE 3 fix: executor CLIs (agy/claude) live under $HOME/.local/bin, which the
# systemd default PATH omits. Ensure the orchestration process can discover them
# so WorkspaceBoundary's sandbox binds/execs them (bwrap: execvp agy fix).
# Derived from ${HOME} (never hardcoded) so the wrapper stays correct for any
# deployment user.
export _ANTIGONA_LOCAL_BIN="${HOME}/.local/bin"
if [[ ":$PATH:" != *":$_ANTIGONA_LOCAL_BIN:"* ]]; then
    export PATH="${_ANTIGONA_LOCAL_BIN}:$PATH"
fi

# ── Identity boundary: strip Hermes contamination (same as run.sh) ─
for _hermes_var in ${!HERMES_@}; do
    unset "$_hermes_var" 2>/dev/null || true
done
unset PYTHONPATH 2>/dev/null || true

# ── Verifier credential / dev token from secrets (same as run.sh) ─
if [ -z "${ANTIGONA_VERIFIER_CREDENTIAL:-}" ] && [ -f "$ROOT/secrets/verifier_credential.json" ]; then
    export ANTIGONA_VERIFIER_CREDENTIAL="$(python3 -c "
import json
print(json.load(open('$ROOT/secrets/verifier_credential.json'))['VERIFIER_CREDENTIAL'])
")"
fi
if [ -z "${ANTIGONA_DEV_TOKENS:-}" ] && [ -f "$ROOT/secrets/gateway_dev_token.json" ]; then
    export ANTIGONA_DEV_TOKENS="$(python3 -c "import json; print(json.load(open('$ROOT/secrets/gateway_dev_token.json'))['DEV_TOKEN'])"):owner-1"
fi

mkdir -p "$ANTIGONA_WORKSPACE"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    echo "Antigona Python environment missing (.venv-new/.venv)" >&2; exit 1
fi
case "${1:-}" in
    gateway)        exec $PY -m antigona.gateway ;;
    verifier)       exec $PY -m antigona.verifier_service ;;
    worker)         exec $PY -c 'from antigona.worker import main; main()' ;;
    delivery)       exec $PY -c 'from antigona.delivery_worker import main; main()' ;;
    bot)            exec $PY -c 'from antigona.channels.telegram.bot import main; main()' ;;
    orchestration)  exec $PY -m antigona.orchestration.service ;;
    *) echo "unknown service: ${1:-}"; exit 2 ;;
esac
