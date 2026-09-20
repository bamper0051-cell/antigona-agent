#!/usr/bin/env bash
# =============================================================================
#  Antigona — провижининг sandbox-образов (best-effort, идемпотентно)
# =============================================================================
#  Первая sandbox-shell-команда запускается в контейнере из python:3.12-slim
#  (worker) / python:3.12-alpine (файловый sandbox). Прокси сокета докера
#  ПО ДИЗАЙНУ запрещает pull (POST /images/create не в allowlist), поэтому
#  отсутствующий образ валит первую команду пользователя (exit 125), а не
#  установку. Этот скрипт пред-пуллит образы через РЕАЛЬНЫЙ сокет.
#
#  Поведение:
#    * docker CLI отсутствует        -> громкое предупреждение + ремедиум, rc=0;
#    * docker-демон недоступен       -> то же;
#    * образ уже есть                -> ok, пропуск (идемпотентно);
#    * образа нет                    -> docker pull; успех ok, провал — громко
#      с именем образа и точной командой `docker pull <img>`;
#    * ANTIGONA_REQUIRE_SANDBOX_IMAGE=1 -> любой из отказов выше => rc!=0.
#  НИКОГДА не использует прокси-сокет /run/antigona/docker.sock (DOCKER_HOST
#  принудительно снимается, docker берёт дефолтный реальный сокет).
#
#  Переменные окружения:
#    ANTIGONA_SANDBOX_IMAGES       список образов (по умолчанию оба выше)
#    ANTIGONA_REQUIRE_SANDBOX_IMAGE=1  жёсткий отказ вместо предупреждения
# =============================================================================
set -euo pipefail

IMAGES="${ANTIGONA_SANDBOX_IMAGES:-python:3.12-slim python:3.12-alpine}"
REQUIRE="${ANTIGONA_REQUIRE_SANDBOX_IMAGE:-0}"

# Pull обязан идти в реальный docker-демон; прокси-сокет запрещает create/pull.
unset DOCKER_HOST

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
crit() { printf '\n\033[31m✗ SANDBOX IMAGES NOT PROVISIONED:\033[0m %s\n' "$*" >&2; }

# Общий ремедиум + opt-in жёсткий отказ.
bail() {
    local msg="$1"
    if [ "$REQUIRE" = "1" ]; then
        crit "$msg — отказ, т.к. ANTIGONA_REQUIRE_SANDBOX_IMAGE=1"
        exit 1
    fi
    exit 0
}

say "Antigona sandbox image provisioning — образы: $IMAGES"

if ! command -v docker >/dev/null 2>&1; then
    warn "docker CLI не найден — sandbox-образы не проверены."
    warn "Первая sandbox-shell-команда УПАДЁТ, пока образы не установлены вручную:"
    warn "   docker pull python:3.12-slim python:3.12-alpine    (реальный сокет)"
    bail "docker CLI отсутствует"
fi

if ! docker info >/dev/null 2>&1; then
    warn "docker-демон недоступен — sandbox-образы не проверены."
    warn "Команда для ручной установки образов:"
    warn "   docker pull python:3.12-slim python:3.12-alpine    (реальный сокет)"
    bail "docker-демон недоступен"
fi

missing=0
for img in $IMAGES; do
    if docker image inspect "$img" >/dev/null 2>&1; then
        ok "образ уже есть: $img"
        continue
    fi
    missing=1
    if docker pull "$img"; then
        ok "образ загружен: $img"
    else
        warn "✗ SANDBOX IMAGE MISSING: $img — первая sandbox-shell-команда УПАДЁТ."
        warn "   Fix: sudo docker pull $img    (реальный сокет, не /run/antigona/docker.sock)"
        if [ "$REQUIRE" = "1" ]; then
            crit "образ $img отсутствует и pull не удался"
            exit 1
        fi
    fi
done

if [ "$missing" = "1" ] && [ "$REQUIRE" != "1" ]; then
    warn "Не все sandbox-образы доступны — shell-команды в sandbox могут падать."
fi
exit 0
