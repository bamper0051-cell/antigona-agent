<div align="center">
  <img src="assets/antigona_banner.png" alt="Antigona" width="400"/>

  # Antigona

  ### Собственный AI-агент — от Telegram-бота до TUI-панели
  
  **Clean-room autonomous agent harness**  
  *Python 3.11+ | asyncio | 363 модуля | 4156 тестовых функций в 401 файле*

  [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
  [![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
  [![CI](https://github.com/bamper0051-cell/antigona-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bamper0051-cell/antigona-agent/actions/workflows/ci.yml)
  [![Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
  [![Mypy strict](https://img.shields.io/badge/mypy-strict-blueviolet.svg)](http://mypy-lang.org/)

</div>

---

> **Release status:** not release-ready.  Capability descriptions and counts
> below are product documentation, not release evidence.  Follow
> [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md), whose commands are bound
> to the exact candidate SHA and the CI workflow.

---

## 📋 Содержание

- [Что такое Antigona?](#-что-такое-antigona)
- [Архитектура](#-архитектура)
- [Возможности](#-возможности)
- [Быстрый старт](#-быстрый-старт)
- [CLI (командная строка)](#-cli-командная-строка)
- [TUI Dashboard](#-tui-dashboard)
- [Telegram-бот](#-telegram-бот)
- [Структура проекта](#-структура-проекта)
- [Дорожная карта](#-дорожная-карта)
- [Тестирование и качество](#-тестирование-и-качество)
- [Лицензия](#-лицензия)

Отдельные документы:
[ARCHITECTURE.md](ARCHITECTURE.md) (архитектура простыми словами) ·
[SECURITY.md](SECURITY.md) (как сообщить об уязвимости) ·
[CONTRIBUTING.md](CONTRIBUTING.md) (как участвовать) ·
[CHANGELOG.md](CHANGELOG.md) (история изменений)

---

## 🚀 Что такое Antigona?

**Antigona** — это **Durable Agent Platform**: многоэтапный, восстанавливаемый, безопасный AI-агент, построенный с нуля.
Задача переживает рестарт процесса — состояние живёт в PostgreSQL, а не в памяти одного воркера.
Antigona объединяет **три канала взаимодействия** в единую платформу:

| Канал | Назначение | Статус |
|-------|-----------|--------|
| 🤖 **Telegram-бот** | Разговорный AI, slash-команды, intent router | ✅ Работает |
| 🖥️ **CLI** | Терминальный AI-агент для разработчиков | ✅ Работает |
| 📊 **TUI Dashboard** | Textual-панель управления задачами | ✅ Работает |

**Текущий статус:** Phase 1.5 — TurnEngine интегрирован в Worker (`turn_bridge`), 23/24 шага основной дорожной карты приняты (P5.2). Подробности: [`CHANGELOG.md`](CHANGELOG.md), [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Ключевые возможности:** TurnEngine (пошаговое read-only исполнение с самопочинкой), Gateway/Worker/Verifier изоляция, durable-состояние в PostgreSQL, multi-channel доставка (Telegram/email через `DeliveryAdapter`).

**Clean-room:** ни одной строки копированного кода Hermes, OpenClaw, Qwen или AGPL-проектов. Только публичные идеи, своя реализация.

---

## 🏗 Архитектура

```
┌─────────────────────────────────────────────────────┐
│                   🌐 CHANNELS                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ Telegram  │  │   CLI    │  │  TUI Dashboard   │   │
│  │   bot     │  │ (typer)  │  │   (Textual)      │   │
│  └─────┬─────┘  └────┬─────┘  └────────┬─────────┘   │
│        └──────────────┼─────────────────┘             │
└───────────────────────┼──────────────────────────────┘
                        │
┌───────────────────────┼──────────────────────────────┐
│              🔧 CORE PIPELINE                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ Transport│─▶│  Router  │─▶│Conversat.│           │
│  └──────────┘  └──────────┘  └────┬─────┘           │
│  ┌──────────┐  ┌──────────┐       │                  │
│  │  Intent  │  │  Memory  │       ▼                  │
│  │  Router  │  │Summarizer│  ┌──────────┐           │
│  └──────────┘  └──────────┘  │  Planner  │           │
│                               └────┬─────┘           │
│  ┌──────────┐  ┌──────────┐       │                  │
│  │  Policy  │  │ Executor │       ▼                  │
│  └──────────┘  └────┬─────┘  ┌──────────┐           │
│                      └───────▶│ Verifier │           │
│                               └──────────┘           │
└──────────────────────────────────────────────────────┘
                        │
┌───────────────────────┼──────────────────────────────┐
│              🗄️ TASK RUNTIME                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ Gateway  │  │  Worker  │  │ Verifier  │           │
│  │ (FastAPI)│  │ (runtime)│  │ (service) │           │
│  └──────────┘  └──────────┘  └──────────┘           │
│  ┌──────────────────────────────────────────────┐    │
│  │           SQLite / PostgreSQL                 │    │
│  │  task_flows · flow_steps · queue_jobs · ...   │    │
│  └──────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────┐    │
│  │         Docker Sandbox (fail-closed)           │    │
│  │  no-net · read-only root · cap-drop · tmpfs   │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────┘
```

### Три процесса

| Процесс | Роль | Креденциал Verifier |
|---------|------|-------------------|
| `antigona-gateway` | Auth, approvals, cancel, enqueue | ❌ Нет |
| `antigona-worker` | Исполняет задачи в Docker sandbox | ❌ Нет |
| `antigona-verifier` | Единственный финализует `→ DONE` | ✅ Есть |

**Ключевой принцип:** только Verifier может перевести задачу в `DONE`.  
Ни Gateway, ни Worker не имеют этого креденциала — это гарантирует, что финализация никогда не произойдёт без верификации.

### Поток исполнения (Phase 1.5 — turn_bridge)

```
Telegram / CLI / Dashboard → InputPipeline → Gateway → Durable TaskFlow → Worker
  ├── Read-only: TurnTaskExecutor (через TurnEngine)
  └── Write/Shell: Orchestrator → SubagentAdapter (Claude/Codex)
    → Observer → Verifier → Delivery
```

`TurnEngine` (донор: Hermes iteration loop, адаптирован в `src/antigona/turn_bridge/`) исполняет read-only задачи пошагово через `TurnTaskExecutor`, с самопочинкой ошибок без участия пользователя. Полная карта потока: [`docs/ARCHITECTURE_MAP.md`](docs/ARCHITECTURE_MAP.md).

---

## ✨ Возможности

### 🤖 Telegram-бот
- Разговорный интеллект: понимает контекст, приветствия, вопросы, шум
- **Intent Router:** 37 типов намерений с порогами уверенности
- Slash-команды: `/start`, `/help`, `/setllm`, `/status`, `/model`, `/skills`
- Контекстная эвристика: «Проверь» после обсуждения → действие
- Gateway degradation: разговор переживает падение Gateway
- Dedup + rate limit: никаких дублей ответов

### 🧠 Ядро
- **Transport / Router / Conversation / Planner / Policy / Executor** — модульная pipeline
- **EventBus:** 11 типов событий с `correlation_id`
- **SQLite-сессии:** WAL mode, crash-safe
- **MemorySummarizer:** turn_buffer, active_topic, session_summary
- **Provider Registry:** Mock + OpenAI-compatible + любой провайдер
- **ContextBuilder:** persona + policy + history + token budget

### 🛠️ Task Runtime
- **Tool contracts + registry:** FilesystemRead, FilesystemWrite, Terminal
- **Policy engine:** risk levels + approval gates + forbidden targets
- **Verifier:** postconditions для file/shell/code tasks — **no fake DONE**
- **Durable state machine:** optimistic lock, revision CAS, sticky cancel
- **DeliveryAdapter:** decouples progress from channel

### 🖥️ CLI + TUI
- `antigona-cli chat` — интерактивный AI-чат с Rich-рендерингом
- prompt_toolkit: история, multiline, syntax highlighting
- **TUI Dashboard:** 6 вкладок, live events, skills manager, delegation UI
- Session UX: list, resume, export, title

### 📊 Качество
- Trace collection: privacy-safe, никаких секретов в логах
- Intent curriculum: 100+ примеров, 22 интента
- Adversarial eval: injection, mixed intents, indirect requests
- Conversation scoring: 4 измерения, weighted overall
- Error-driven learning: каждая коррекция → regression case

---

## ⚡ Быстрый старт

### Установка (один клик)

```bash
# Клонировать
git clone https://github.com/bamper0051-cell/antigona-agent.git
cd antigona

# Поставить окружение и зависимости (venv + pip install -e), без sudo
bash install.sh

# Нужны pytest/ruff/mypy для разработки:
# bash install.sh --dev
```

`install.sh` проверяет Python ≥ 3.11 и **останавливается** с понятным сообщением,
если его нет; создаёт `.venv` внутри клона; ставит зависимости из `pyproject.toml`;
затем **проверяет** установку реальным импортом пакета и запуском CLI. Секреты он
не создаёт и не требует, `sudo` не использует, повторный запуск ничего не ломает.
Пока проверка не прошла, скрипт не печатает «установлено».

**Sandbox-образы (обязательное условие shell-команд).** Первая shell-команда
запускается в контейнере из `python:3.12-slim` (worker) и `python:3.12-alpine`
(файловый sandbox). Прокси сокета докера **по дизайну** запрещает pull
(`POST /images/create` не в allowlist), поэтому образы надо пред-пуллить на хосте
через **реальный** сокет, а не `/run/antigona/docker.sock`:

```bash
docker pull python:3.12-slim python:3.12-alpine
```

`install.sh` проверяет это best-effort на шаге провижининга и громко
предупреждает, если образа нет; жёсткий отказ прямо на этапе установки
включается переменной `ANTIGONA_REQUIRE_SANDBOX_IMAGE=1`.

### Проверка, что всё работает

```bash
source .venv/bin/activate
antigona --version    # версия пакета
antigona --help       # список команд
```

### Запуск CLI

```bash
antigona run "..."    # отправить задачу через Gateway
antigona chat         # интерактивная сессия (нужен запущенный Gateway)
```

### Запуск TUI

```bash
antigona-tui
```

### Запуск сервисов (gateway / worker / verifier / delivery)

Сервисы требуют настроенного хранилища и переменных окружения — порядок и точные
команды: [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md),
установка systemd-юнитов: [`deploy/systemd/install_units.sh`](deploy/systemd/install_units.sh).

Deployment-путей два, и они не взаимозаменяемы. `deploy/systemd/*.service` — это
18-строчные legacy-шаблоны; реально работающая live-связка — 7 hardened-юнитов
(58–61 строка, `User=antigona-svc`, `ProtectSystem=strict`, `NoNewPrivileges=yes`)
в `/etc/systemd/system`, и installer теперь **отказывается** затирать отличающийся
target без `--force` (с `--force` — сначала бэкап в `--backup-dir`, только потом
запись). Fail-closed startup-gate (C11-манифест) добавляется к уже существующим
юнитам через drop-in `deploy/systemd/dropins/10-antigona-startup-gate.conf`
(`install_units.sh --dropins`), который сливается с юнитом и не ломает sandbox;
режим `--check` показывает drift, ничего не записывая. В drop-in-режиме отличающийся
(hardened) базовый юнит не является ошибкой: он пропускается байт-в-байт, а drop-in
накладывается поверх (drop-in **сливается** с юнитом и не заменяет его). Нужен режим,
который вообще не трогает базовые юниты — `install_units.sh --dropins-only` (алиас
`--no-base-units`) пишет только `<unit>.service.d/10-antigona-startup-gate.conf`.
Установка drop-in, а равно
`daemon-reload`/restart для его активации — **ручной шаг, одобряемый владельцем**;
installer сам этого не делает.

**Обязательное условие запуска (измерено).** Шиппинговый drop-in
[`deploy/systemd/dropins/10-antigona-startup-gate.conf`](deploy/systemd/dropins/10-antigona-startup-gate.conf)
добавляет в каждый юнит `ExecStartPre=<root>/scripts/startup_gate.sh`. Этот гейт
запускает `python3 -m antigona.startup.validator --check=manifest`, который берёт
deployment-envelope из переменной окружения `ANTIGONA_DEPLOYMENT_MANIFEST` либо, по
умолчанию, из `<code-root>/CANDIDATE_DEPLOYMENT_MANIFEST.json`
(см. `src/antigona/startup/validator.py:181`). Сам envelope **не входит** в публикуемое
дерево — его поставляет оператор. Поэтому на чистом source-checkout / публичном клоне
без envelope гейт завершается с `rc=1` и строкой
`CRITICAL: startup gate FAIL-CLOSED - immutability validation (--check=manifest)`,
а systemd **отменяет** запуск юнита (измерено: `bash scripts/startup_gate.sh` → rc=1).
Единственный документированный обход — `ANTIGONA_SKIP_STARTUP_GATE=1` (громкий, виден
в journal), либо предоставить envelope и указать на него `ANTIGONA_DEPLOYMENT_MANIFEST`.
Подробнее — ADR-003, раздел «Operational prerequisite».

---

## 🖥️ CLI (командная строка)

Antigona CLI построен на **Typer** с поддержкой операций Gateway.
Команды, требующие Gateway, берут адрес и Bearer-токен из `--gateway`/`--token`
или из окружения/`.env` (`ANTIGONA_GATEWAY_URL`, `ANTIGONA_GATEWAY_TOKEN`).

```bash
antigona --help                  # все команды
antigona <COMMAND> --help        # справка по конкретной команде

# Задачи
antigona run "<goal>"            # создать задачу через Gateway
antigona attach <flow_id>        # подключиться к задаче и следить за ней
antigona cancel <flow_id>        # отменить задачу

# Approvals
antigona approve <approval_id>          # одобрить
antigona approve <approval_id> --deny   # отклонить

# Cron-расписания
antigona cron create --name <имя> --cron "0 9 * * *" --goal "<цель>"
antigona cron list
antigona cron cancel <schedule_id>

# Replay-просмотр
antigona replay <flow_id> --timeline   # плоская хронология (есть --json)

# Навыки и durable-хранилище
antigona skills list
antigona db upgrade              # alembic upgrade head
```

### Интерактивный AI-чат

Тот же CLI-агент в режиме интерактивного общения (нужен запущенный Gateway):

```bash
antigona chat            # то же самое: antigona-cli chat
```

Фишки:
- 🔄 Streaming токенов в реальном времени
- 📜 История команд через prompt_toolkit
- ⌨️ Slash-команды: `/help`, `/model`, `/status`, `/new`, `/resume`
- 🎨 Rich-рендеринг + ASCII-лого

---

## 📊 TUI Dashboard

`antigona-tui` — это **Textual**-панель управления:

```
┌──────────────────────────────────────────────────┐
│ [Flows] [Live] [Approvals] [Replay] [Skills] ... │
├──────────────────────────────────────────────────┤
│                                                  │
│  📋 Flows tab: таблица активных задач            │
│  🔴 Live tab: WebSocket-лента событий в реальном │
│     времени                                      │
│  ✅ Approvals: кнопки approve/reject             │
│  🔍 Replay: timeline + step tree                 │
│  🧩 Skills: загрузка, permissions, статус        │
│  🤖 Delegation: Claude/Codex адаптеры            │
│                                                  │
│  📊 Status bar: Gateway / Bot / Verifier health   │
│  🎨 Цветовая схема: зелёный→успех, красный→      │
│     ошибка, жёлтый→REJECTED, серый→CANCELLED      │
└──────────────────────────────────────────────────┘
```

Запуск:
```bash
antigona-tui
```

---

## 🤖 Telegram-бот

Бот **@AntigonaAI_bot** — основной интерфейс для пользователей:

- **Conversation Engine:** понимает естественный язык, контекст, интенты
- **Intent Router:** 37 типов намерений, пороги уверенности
- **Provider-agnostic:** DeepSeek, OpenRouter, любой OpenAI-compatible
- **Gateway-интеграция:** создаёт `task_flows`, подписывается на прогресс
- **Context window:** персона + политики + история + token budget

---

## 📁 Структура проекта

```
antigona/
├── src/antigona/              # Основной код
│   ├── gateway/               # FastAPI Gateway (auth/approvals/cancel)
│   ├── worker/                # Worker runtime + tools
│   ├── channels/telegram/     # Telegram-бот
│   ├── conversation/          # Разговорный движок
│   ├── memory/                # MemorySummarizer
│   ├── router/                # Intent Router
│   ├── planner/               # Task Planner
│   ├── executor/              # Tool Executor
│   ├── policy/                # Policy engine
│   ├── verifier/              # Completion verifier
│   ├── sandbox/               # Docker sandbox
│   ├── durable/               # State machine, state transitions
│   ├── sessions/              # Session management
│   ├── skills/                # Skill system (P2)
│   ├── providers/             # LLM providers registry
│   ├── delivery/              # DeliveryAdapter
│   ├── egress/                # Egress proxy
│   ├── transport/             # Transport layer
│   ├── tools/                 # Tool contracts
│   ├── sql/                   # SQL utilities
│   ├── events/                # EventBus
│   ├── cli.py                 # Typer CLI
│   ├── tui.py                 # Textual TUI
│   ├── models.py              # ORM models
│   ├── database.py            # Database layer
│   ├── repository.py          # Task repository
│   ├── config.py              # Configuration
│   └── ...                    # 363 модуля
├── tests/                     # тесты
│   ├── unit/                  # Unit-тесты
│   ├── integration/           # Интеграционные тесты
│   └── security/ sandbox/ …   # + arch, architecture, chaos, characterization,
│                              #   gateway, packaging, recovery
├── docs/                      # Документация
│   ├── ARCHITECTURE.md        # Архитектура P0
│   ├── ROADMAP.md             # Дорожная карта
│   └── adr/                   # Architecture Decision Records
├── pyproject.toml             # Проект Python
├── install.sh                 # Установка одним кликом (venv + зависимости + проверка)
├── deploy/systemd/            # systemd-юниты и install_units.sh
└── README.md                  # Этот файл
```

---

## 🗺️ Дорожная карта

Проект развивается по 45-дневному плану (два трека):

### Трек A: CLI-агент
| Статус | Этап |
|--------|------|
| ✅ | Интерактивный чат, streaming, history |
| ✅ | Slash-команды, session UX |
| 🔄 | Расширенные тулы, MCP |
| ⬜ | Система скиллов и делегирования |

### Трек B: Разговорный интеллект
| Статус | Этап |
|--------|------|
| ✅ | Telegram-бот, conversation engine |
| ✅ | Intent Router (37 типов) |
| ✅ | Memory, sessions, provider integration |
| 🔄 | Context builder, quality pass, adversarial eval |

### P0-P5 Общий прогресс
| Фаза | Статус | Что внутри |
|------|--------|-----------|
| **P0** Core | ✅ 85% | Gateway / Worker / Verifier, state machine, sandbox |
| **P1** Memory | ✅ Done | PostgreSQL memory with pgvector |
| **P2** Skills | ✅ Done | ASKILL format, lifecycle, matcher, cron, replay |
| **P3** CLI+TUI | ✅ Done | Typer CLI, Textual dashboard |
| **P4** Production | 🔄 | Postgres/Redis durable, egress proxy, multi-tenant |
| **P5** Hermes-clone | ⬜ | Система скиллов, delegation, browser |

> Полная дорожная карта: [`docs/ROADMAP.md`](docs/ROADMAP.md)

---

## 🧪 Тестирование и качество

```bash
# Unit-тесты
pytest tests/unit -x --tb=short

# Все тесты (кроме Postgres)
pytest --ignore=tests/integration -x --tb=short

# Линтеры
ruff check src/
mypy src/

# Покрытие (порог: 85%)
coverage run -m pytest tests/unit
coverage report
```

**Метрики качества:**
- 4156 тестовых функций в 401 файле (последний полный локальный прогон: 4907 passed / 51 skipped / 3 xfailed, 0 failed)
- Branch coverage ≥ 85%
- Mypy strict mode
- Ruff (E, F, I, B, UP)
- Adversarial eval: injection, mixed intents, indirect requests
- Conversation scoring: 4 измерения

---

## 🔒 Безопасность

- **Fail-closed верификация (BAM-6)**: составные цели (multi_file / file_write_read) завершаются `DONE` только если КАЖДЫЙ требуемый файл реально существует в workspace как обычный файл (не symlink, не hardlink, не в symlinked-каталоге). Частично выполненная операция → `FAILED`, никогда не `DONE` (инвариант: `required_success_count == required_operation_count`).
- **Owner/approval**: MEDIUM+ действия требуют явного одобрения владельца; fail-closed без identity (`user_id` обязателен).
- **Песочница**: высокорисковые shell-команды только в изолированном Docker-контейнере (bind-mount workspace, no-new-privileges, cap-drop); host-исполнение ограничено P0-allowlist без `rm/cp/mv/sed/find`.
- **DONE ≠ proof**: верификатор сверяет артефакт на диске (SHA256 + содержимое), не доверяя self-report исполнителя; CAS-переход `VERIFYING → DONE` — только у verifier-сервиса.
- **Partial-fail propagation (BAM-5)**: «затем/потом»-цели декомпозируются; сбой второго шага не маскируется — глобальный статус никогда не unconditional `DONE`.
- **Секреты**: ключи только из env/файла секретов, никогда в отчётах/архивах; sticky cancel; send-only delivery.

## 🏭 Сервисы (6)

| Сервис | Роль |
|--------|------|
| `antigona-bot` | Telegram-канал |
| `antigona-gateway` | HTTP API + approval flow + /status |
| `antigona-orchestration` | планирование/роутинг/аггрегация |
| `antigona-worker` | исполнение задач (durable queue) |
| `antigona-verifier` | независимая верификация DONE (CAS) |
| `antigona-delivery` | доставка результатов/отчётов |

## 📜 Лицензия

**Apache License 2.0** — свободно используйте, модифицируйте, распространяйте.

---

<div align="center">
  <sub>Built with ❤️ and 🐍 Python 3.11+</sub>
  <br>
  <sub>© 2026 Antigona Project</sub>
</div>
