#!/usr/bin/env bash
# =============================================================================
#  Antigona — установка одним кликом (Linux / macOS)
# =============================================================================
#  Что делает скрипт:
#    1) проверяет Python >= 3.11 и ПАДАЕТ с понятным сообщением, если его нет;
#    2) создаёт виртуальное окружение в .venv/ (внутри клона);
#    3) ставит зависимости из pyproject.toml (editable-установка пакета);
#    4) ПРОВЕРЯЕТ установку реальным импортом и запуском CLI;
#    5) ПРОВИЖИНИТ sandbox-образы (python:3.12-slim / python:3.12-alpine):
#       если их нет — ГРОМКО предупреждает с точной командой `docker pull ...`
#       (best-effort по умолчанию; жёсткий отказ — ANTIGONA_REQUIRE_SANDBOX_IMAGE=1);
#    6) печатает, что делать дальше.
#
#  Чего скрипт НЕ делает (сознательно):
#    * не использует sudo и не пишет ничего вне каталога клона;
#    * не делает глобальных pip install;
#    * не создаёт, не запрашивает и не выдумывает секреты;
#    * не требует платных сервисов (pull идёт в реальный docker-сокет, не в прокси);
#    * НЕ печатает «установлено успешно», пока это не подтверждено проверкой.
#
#  Использование:
#    bash install.sh                 # базовые зависимости
#    bash install.sh --dev           # + extras [dev] (pytest, ruff, mypy)
#    bash install.sh --extras tui,voice
#    bash install.sh --force         # переустановить, игнорируя stamp
#    bash install.sh --help
#
#  Переменные окружения:
#    ANTIGONA_VENV=/path/to/venv     # куда ставить окружение (по умолчанию <клон>/.venv)
#    ANTIGONA_PYTHON=/path/to/python # какой интерпретатор использовать
#    ANTIGONA_INSTALL_EXTRAS=dev,tui # extras (аналог --extras)
#    ANTIGONA_REQUIRE_SANDBOX_IMAGE=1 # без sandbox-образа — отказать (по умолчанию 0)
# =============================================================================
set -euo pipefail

MIN_PY_MAJOR=3
MIN_PY_MINOR=11

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ANTIGONA_VENV:-$ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python"
CLI_BIN="$VENV_DIR/bin/antigona"
STAMP_FILE="$VENV_DIR/.antigona_install_stamp"

EXTRAS="${ANTIGONA_INSTALL_EXTRAS:-}"
FORCE=0

# --- helpers ----------------------------------------------------------------

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
fail() { printf '\n\033[31m✗ УСТАНОВКА НЕ ЗАВЕРШЕНА:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

py_at_least_311() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" \
        >/dev/null 2>&1
}

# --- args -------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --dev)      EXTRAS="${EXTRAS:+$EXTRAS,}dev" ;;
        --extras)   [ $# -ge 2 ] || fail "--extras требует аргумент, напр. --extras tui,voice"
                    EXTRAS="${EXTRAS:+$EXTRAS,}$2"; shift ;;
        --force)    FORCE=1 ;;
        -h|--help)  usage ;;
        *)          fail "Неизвестный аргумент: $1 (см. bash install.sh --help)" ;;
    esac
    shift
done

say "Antigona installer · каталог: $ROOT"
[ -f "$ROOT/pyproject.toml" ] || fail "рядом со скриптом нет pyproject.toml — запускайте install.sh из корня клона Antigona"

if [ -n "${SUDO_USER:-}" ]; then
    warn "скрипт запущен через sudo — это не нужно. Установка идёт только в $VENV_DIR."
fi

# --- 1. Python >= 3.11 (fail-closed) ----------------------------------------

step "1/4 Проверка Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"

PYTHON_BIN=""
PYTHON_VERSION=""
SEEN=""

if [ -n "${ANTIGONA_PYTHON:-}" ]; then
    [ -x "$ANTIGONA_PYTHON" ] || fail "ANTIGONA_PYTHON=$ANTIGONA_PYTHON не найден или не исполняемый"
    py_at_least_311 "$ANTIGONA_PYTHON" || fail "ANTIGONA_PYTHON=$ANTIGONA_PYTHON младше ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
    PYTHON_BIN="$ANTIGONA_PYTHON"
    PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
fi

if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        path="$(command -v "$candidate" 2>/dev/null || true)"
        [ -n "$path" ] || continue
        version="$("$path" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo "?")"
        SEEN="${SEEN}${SEEN:+, }$candidate ($version)"
        if py_at_least_311 "$path"; then
            PYTHON_BIN="$path"
            PYTHON_VERSION="$version"
            break
        fi
    done
fi

if [ -z "$PYTHON_BIN" ]; then
    {
        echo
        echo "Найденные интерпретаторы: ${SEEN:-ни одного}"
        echo
        echo "Нужен Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} или новее — Antigona на более старых версиях не запустится."
        echo "Как поставить (команды выполняете ВЫ, скрипт сам sudo не использует и не просит):"
        echo "  Debian/Ubuntu:  sudo apt-get install python3.11 python3.11-venv"
        echo "  Fedora/RHEL:    sudo dnf install python3.11"
        echo "  macOS (brew):   brew install python@3.11"
        echo "Либо укажите готовый интерпретатор: ANTIGONA_PYTHON=/path/to/python3.11 bash install.sh"
    } >&2
    fail "подходящий Python (>= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}) не найден — ничего не изменено"
fi

ok "интерпретатор: $PYTHON_BIN ($PYTHON_VERSION)"

# --- 2. venv (идемпотентно) -------------------------------------------------

step "2/4 Виртуальное окружение"
VENV_REUSED=0

if [ -x "$VENV_PY" ]; then
    py_at_least_311 "$VENV_PY" \
        || fail "$VENV_DIR существует, но его интерпретатор младше ${MIN_PY_MAJOR}.${MIN_PY_MINOR}.
   Скрипт не удаляет каталоги. Удалите вручную или укажите другое место:
     rm -rf \"$VENV_DIR\"   (или)   ANTIGONA_VENV=/path/to/venv bash install.sh"
    VENV_REUSED=1
    ok "использую существующее окружение: $VENV_DIR"
elif [ -e "$VENV_DIR" ]; then
    fail "$VENV_DIR уже существует, но не выглядит как рабочее venv (нет $VENV_PY).
   Скрипт не удаляет чужие каталоги. Разберитесь вручную или задайте ANTIGONA_VENV."
else
    "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "не удалось создать venv в $VENV_DIR
   На Debian/Ubuntu обычно не хватает пакета: sudo apt-get install python3-venv"
    ok "создано окружение: $VENV_DIR"
fi

"$VENV_PY" -c 'import sys; print("  · venv python:", sys.version.split()[0])'

# --- 3. зависимости из pyproject (без sudo, без глобального pip) -------------

step "3/4 Зависимости из pyproject.toml"

EXTRAS_SPEC=""
if [ -n "$EXTRAS" ]; then
    EXTRAS_SPEC="[$EXTRAS]"
fi
INSTALL_TARGET="$ROOT$EXTRAS_SPEC"

PYPROJECT_HASH="$(sha256_of "$ROOT/pyproject.toml")"
STAMP_VALUE="pyproject=$PYPROJECT_HASH extras=${EXTRAS:-none} python=$PYTHON_VERSION"

NEED_INSTALL=1
if [ "$FORCE" -eq 0 ] && [ "$VENV_REUSED" -eq 1 ] && [ -f "$STAMP_FILE" ] \
   && [ "$(cat "$STAMP_FILE")" = "$STAMP_VALUE" ]; then
    if "$VENV_PY" -c 'import antigona' >/dev/null 2>&1; then
        NEED_INSTALL=0
        ok "зависимости уже стоят (stamp совпадает) — повторная установка не нужна"
    else
        warn "stamp совпадает, но импорт пакета не проходит — переустанавливаю"
    fi
fi

if [ "$NEED_INSTALL" -eq 1 ]; then
    if [ "${ANTIGONA_UPGRADE_PIP:-0}" = "1" ]; then
        say "  · обновляю pip"
        "$VENV_PY" -m pip install --upgrade --disable-pip-version-check --no-input pip \
            || fail "pip не смог обновиться (нет сети? прокси?)"
    fi
    say "  · pip install -e \"$INSTALL_TARGET\"  (только PyPI, без sudo)"
    if ! "$VENV_PY" -m pip install -e "$INSTALL_TARGET" \
            --disable-pip-version-check --no-input; then
        fail "установка зависимостей не удалась (сеть/PyPI/компилятор). Окружение оставлено как есть, ничего не сломано."
    fi
    ok "зависимости установлены"
fi

# --- 4. ПРОВЕРКА (до любого заявления об успехе) ----------------------------

step "4/4 Проверка установки (реальный импорт и запуск CLI)"

PACKAGE_VERSION="$("$VENV_PY" -c 'import antigona; print(antigona.__version__)' 2>/dev/null)" \
    || fail "импорт пакета antigona не работает:
$("$VENV_PY" -c 'import antigona' 2>&1)"
ok "python -c 'import antigona' → OK (версия пакета: $PACKAGE_VERSION)"

[ -x "$CLI_BIN" ] || fail "консольная команда $CLI_BIN не появилась — установка неполная"

CLI_VERSION_OUT="$("$CLI_BIN" --version 2>&1)" \
    || fail "запуск '$CLI_BIN --version' упал:
$CLI_VERSION_OUT"
ok "antigona --version → $CLI_VERSION_OUT"

CLI_HELP_OUT="$("$CLI_BIN" --help 2>&1)" \
    || fail "запуск '$CLI_BIN --help' упал:
$CLI_HELP_OUT"
ok "antigona --help → OK ($(printf '%s\n' "$CLI_HELP_OUT" | wc -l) строк справки)"

# stamp пишем только после успешной проверки
printf '%s\n' "$STAMP_VALUE" > "$STAMP_FILE" 2>/dev/null || warn "не удалось записать stamp (не критично)"

# --- 5. Провижининг sandbox-образов (best-effort; hard-fail при REQUIRE=1) ----

step "Провижининг sandbox-образов (python:3.12-slim / python:3.12-alpine)"

PROVISION_SCRIPT="$ROOT/deploy/sandbox/provision_sandbox_images.sh"
if [ -f "$PROVISION_SCRIPT" ]; then
    if ! bash "$PROVISION_SCRIPT"; then
        if [ "${ANTIGONA_REQUIRE_SANDBOX_IMAGE:-0}" = "1" ]; then
            fail "sandbox-образы не провизионены, а ANTIGONA_REQUIRE_SANDBOX_IMAGE=1 — установка не завершена"
        fi
        warn "провижининг sandbox-образов не завершился — см. сообщения выше"
    fi
else
    warn "не найден $PROVISION_SCRIPT — sandbox-образы не проверены"
fi

# --- Итог -------------------------------------------------------------------

printf '\n\033[1m=========================================================\033[0m\n'
printf '\033[32mУСТАНОВКА ПРОВЕРЕНА\033[0m — импорт пакета и запуск CLI подтверждены выше.\n'
printf '\033[1m=========================================================\033[0m\n'

cat <<EOF

Что дальше:

  1) Справка по командам:
       $CLI_BIN --help
     Или активировать окружение и звать коротко:
       source "$VENV_DIR/bin/activate"   &&   antigona --help

  2) Секреты: этим скриптом НЕ создаются и НЕ требуются.
     Базовый CLI работает без ключей. Если нужны реальные провайдеры / каналы
     (LLM, Telegram, email) — создайте файл .env в корне клона (он в .gitignore)
     и заполните его сами, точными именами переменных:
       ANTIGONA_API_BASE_URL, ANTIGONA_API_KEY     — OpenAI-совместимый провайдер
       ANTIGONA_DATABASE_URL                       — Postgres/SQLite URL durable-хранилища
       ANTIGONA_GATEWAY_URL, ANTIGONA_GATEWAY_TOKEN — адрес и токен Gateway
       ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN, ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID
     Полный список источников: contracts/config.example.yaml, src/antigona/config.py.
     Никогда не коммитьте .env и не вставляйте ключи в issues/логи.

  3) Полный стек сервисов (gateway/worker/verifier/delivery) и прогон качества:
       docs/RELEASE_RUNBOOK.md
       deploy/systemd/install_units.sh --help

  4) Архитектура и правила проекта: README.md, ARCHITECTURE.md, CONTRIBUTING.md.

Откат: удалите каталог $VENV_DIR — скрипт больше нигде ничего не создавал.
EOF
