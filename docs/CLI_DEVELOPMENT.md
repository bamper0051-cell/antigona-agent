# Antigona — CLI: как устроено, команды, как расширять

> Документ по реальному коду дерева `/var/lib/antigona` (канон, single-core AntigonaBrain на `:8090`).
> Источники истины: `src/antigona/cli.py`, `src/antigona/cli_ui/`, `src/antigona/tools/registry.py`,
> `src/antigona/conversation/dialogue_engine.py`.

---

## 1. Архитектура CLI

CLI Antigona — **тонкий thin-клиент** над Gateway. Сам по себе он не владеет
сессиями, памятью, провайдерами, тулами и БД — всё это живёт на сервере (AntigonaBrain `:8090`).
Единственный канал — `GatewayClient` (HTTP/WS, `antigona.core.gateway_client`).

```
+-------------------+   HTTP/WS    +--------------------+   registry   +----------------+
| antigona CLI      | ------------> | Gateway / AntigonaBrain | ----------> | ToolRegistry    |
| (thin client)     |  /api/v1/... | (классификация,      |   dispatch   | (builtins +    |
| cli_ui/*          |              |  маршрутизация)      |              |  интеграции)   |
+-------------------+              +--------------------+              +----------------+
```

Ключевой принцип: CLI **не считает результат сам** и **не выдумывает успех**.
Всё, что отображается, приходит с сервера. Терминальные состояния:
`DONE / FAILED / BLOCKED / CANCELLED / TIMEOUT / POLICY_DENIED`.

Точка входа: `pyproject.toml` → `[project.scripts]`:

| Команда | Модуль |
|---|---|
| `antigona` / `antigona-cli` | `antigona.cli:main` |
| `antigona-gateway` | `antigona.gateway:main` |
| `antigona-worker` | `antigona.worker:main` |
| `antigona-verifier` | `antigona.verifier_service:main` |
| `antigona-delivery` | `antigona.delivery_worker:main` |
| `antigona-tui` | `antigona.tui:main` |
| `antigona-tui-control` | `antigona.tui_control:main` |
| `antigona-console` | `antigona.tui_console:main` |
| `antigona-bot` | `antigona.channels.telegram.bot:main` |
| `antigona-api` | `antigona.api.server:main` |

---

## 2. Структура пакета CLI

```
src/antigona/
├── cli.py                  # typer-приложение: run/attach/cancel/approve/skills/cron/replay/tui/panel/chat
├── cli_ui/
│   ├── chat.py             # ChatController — живой чат-цикл поверх Gateway
│   ├── layout.py           # AntigonaLayout — full-screen prompt_toolkit (3 зоны + меню)
│   ├── layout_renderer.py  # LayoutRendererAdapter (prompt_toolkit, БЕЗ Rich во время работы)
│   ├── commands.py         # parse_command / dispatch_command — строгий fail-closed парсер
│   ├── command_menu.py     # slash-меню, карточки команд, merge_catalog
│   ├── approval_picker.py  # интерактивный пикер подтверждений (не показывает approval_id)
│   ├── prompts.py          # PromptSession, DEFAULT_SLASH_COMMANDS, SlashCommand
│   ├── status.py           # рендеры списков/статусов/health/memory
│   ├── status_bar.py       # build_pipeline_bar (нижняя панель)
│   ├── flow_adapter.py     # adapt_flow_status
│   ├── models.py           # ChatMessage, ChatUIState, TerminalOutcome и пр.
│   ├── panel.py            # Rich-панель (до full-screen layout)
│   └── activity.py         # get_tracker — живая активность
├── core/
│   ├── gateway_client.py   # GatewayClient (единственный канонический клиент)
│   ├── command_registry.py
│   └── paths.py            # пути к state-файлам
└── tools/
    ├── registry.py         # ToolRegistry + register_builtins()
    ├── contracts.py        # Tool ABC
    └── <tool>.py           # отдельные тулы (см. раздел 6)
```

---

## 3. Интерактивный чат: `antigona chat`

Запуск живого чата с Gateway:

```bash
antigona chat \
  --gateway http://127.0.0.1:8090 \
  --token <ANTIGONA_GATEWAY_TOKEN> \
  --session-id cli-session \
  [--no-anim]
```

Токен берётся из: `--token` → `ANTIGONA_GATEWAY_TOKEN` env → `.env` (строка `ANTIGONA_GATEWAY_TOKEN=`).
Без токена — `Error: Gateway token is required` и `exit(1)`.

URL: `--gateway` → `ANTIGONA_GATEWAY_URL` env → `http://127.0.0.1:8090`.

### Как работает чат
1. `chat()` создаёт `CoreGatewayClient`, `CliRenderer`, `ChatController`.
2. `_run_chat_with_layout`:
   - рендерит стартовую панель, стартует монитор;
   - тянет каталог команд `gateway.list_commands()`;
   - `merge_catalog()` объединяет серверные команды + локальные UI-команды;
   - строит `AntigonaLayout` и `layout.run()`.
3. Свободный текст — это **ход (turn)**: естественное одобрение → один вызов
   `/api/v1/dialogue/turn`. Возвращённый `response_type` решает рендер:
   - `conversation/clarification` → обычный ответ;
   - `task_accepted` → live-ожидание возвращённого flow;
   - `control/error` → ответ.
4. Пока решение на подтверждении, можно одобрить/отклонить естественным языком
   («да» / «нет») без ID. Слэш-команды (`/list`, `/status`, `/approve`, `/deny`) — явный фолбэк.
5. Если Gateway недоступен — CLI **fail-closed** с честным сообщением, локальный движок НЕ поднимает.

### Full-screen layout (AntigonaLayout)
Три зоны сверху вниз:
- **header** — фиксированный 2-строчный баннер (лицо + identity + connection; status + активный flow + индикатор новых событий).
- **center** — скроллящаяся история чата (единственная ресайзящаяся зона).
- **status** — фиксированная 1-строка пайплайна (фаза + реальные события + живая активность).
- **input** — 1-строчный TextArea.
- **menu** — Float-оверлей над status (виден, пока ввод начинается с `/`): компактный список команд + help-карточка выбранной.

Контракт рендера (Termux/SSH/узкий терминал/resize-safe):
- prompt_toolkit владеет альтернативным экраном и курсором; **Rich не используется** во время работы (источник старого фликера и CPR-шума).
- Content-геттеры **чистые**: не мутируют состояние при репаинте.
- Анимации нет — только спиннер в статус-баре (4 Hz) пока идёт реальная работа, и стоп по завершении.
- Ручная прокрутка вверх замораживает viewport (`auto_follow=False`), в header появляется `↓N новых`.

---

## 4. Слэш-команды чата

Серверные + локальные UI. `merge_catalog()` мёржит и дедуплицирует.

| Команда | Действие |
|---|---|
| `/help` | Все команды |
| `/exit`, `/quit` | Выход |
| `/status <flow_id>` | Состояние задачи |
| `/list`, `/tasks` | Активные задачи |
| `/get <flow_id>` | Задача по ID |
| `/cancel <flow_id>` | Отменить задачу |
| `/steer <flow_id> <текст>` | Изменить направление задачи |
| `/approvals` | Ожидающие подтверждения |
| `/approve <flow_id>` | Одобрить действие |
| `/deny <flow_id>` | Отклонить действие |
| `/health` | Проверка Gateway |
| `/commands` | Реестр команд |
| `/session <id>` | Текущая сессия |
| `/history <id>` | История сессии |
| `/memory [текст]` | Память агента |
| `/clear` (локальная) | Очистить экран |
| `/theme <neon\|minimal>` (локальная) | Сменить тему |

Парсер `parse_command()` — **fail-closed**: неизвестная команда, мусорные аргументы,
некорректный resource_id → `MALFORMED/UNSUPPORTED/UNKNOWN`. `resource_id`
валидируется строго (`^[a-zA-Z0-9_-]{1,36}$`, без слэшей/доттраверсала/контрола).

### Как добавить локальную слэш-команду
1. Добавь её в `parse_command()` (`cli_ui/commands.py`) → новый `CommandKind` (если нужно) + `case "/имя":`.
2. Добавь обработку в `dispatch_command()` → `GATEWAY_EXECUTION` или `LOCAL_ACTION`.
3. Зарегистрируй в `DEFAULT_SLASH_COMMANDS` (`cli_ui/prompts.py`) и/или `LOCAL_UI_COMMANDS` (`cli_ui/command_menu.py`), чтобы она появилась в меню и autocomplete.
4. Напиши unit-тест (`tests/unit/test_cli_command_parsing.py`).

---

## 5. Не-интерактивные команды `antigona`

| Команда | Назначение | Ключевые флаги |
|---|---|---|
| `antigona run -g "<goal>"` | Запустить задачу на Gateway | `-p/--path`, `-c/--content`, `-t/--tool`, `--command`, `--gateway`, `--token`, `--attach/--no-attach` |
| `antigona attach` | Приаттачить live WS-стрим к flow | `--gateway`, `--token` |
| `antigona cancel <flow_id>` | Отменить flow | `--gateway`, `--token` |
| `antigona approve <flow_id> --yes/--no \| --deny` | Одобрить/отклонить | `--gateway`, `--token` |
| `antigona validate-skill` | Валидировать скилл | — |
| `antigona list-skills` | Список скиллов | `--gateway`, `--token` |
| `antigona show-skill` | Показать скилл | `--gateway`, `--token` |
| `antigona promote-skill -r <rev>` | Продвинуть скилл | `--gateway`, `--token` |
| `antigona capture-skill` | Захватить скилл | `--gateway`, `--token`, `--state-root` |
| `antigona deprecate-skill` | Пометить скилл устаревшим | `--gateway`, `--token` |
| `antigona cron-create -n ... -c ... -g ...` | Создать cron | `--cron` (5 полей), `-p/--path`, `--gateway`, `--token` |
| `antigona cron-list` / `cron-show` / `cron-jobs` / `cron-cancel` / `cron-tick` | Управление cron | `--gateway`, `--token` |
| `antigona replay <id>` | Хронология переходов | `--timeline`, `--json`, `--actor`, `--entity-type`, `--from`, `--to`, `--gateway`, `--token` |
| `antigona db-upgrade` | Миграции БД | `--db-url` |
| `antigona tui` | TUI-дашборд | `--gateway`, `--token`, `--refresh` |
| `antigona panel` | Rich-панель | `--gateway`, `--token`, `--refresh`, `-s/--session-id` |

---

## 6. Тулы: registry и интеграции

### Встроенные тулы (`register_builtins` в `tools/registry.py`)
| Тула | Toolset |
|---|---|
| `write_file` (path, content) | filesystem |
| `send_file` (path) | filesystem |
| `run_shell` (command) | shell |
| `generate_image` | media |
| `configure_key` | system |
| `memorize` | memory |
| `count_tokens` (text) | system |
| `kanban` | productivity |
| `loop` (max_iterations) | system |
| `rss` (url, limit) | web |
| `mcp` | system |
| `acp` | system |
| `tmux` (owner-gated, `_owner_id`) | system |

Регистрация: `registry.register(name, toolset=..., schema=..., handler=..., check_fn=..., requires_env=...)`.
Плюс legacy `ToolABC`-контракты (`contracts.py`). Авто-дискавери: `registry.discover()` импортирует
`antigona.tools.*` модули с top-level `register()`.

### Интеграционные тулы (модель-вызываемые через `⟪tool:NAME key="val"⟫`)
В `DialogueEngine._INTEGRATION_TOOLS` (conversation/dialogue_engine.py):

```
kanban      — create/move/done/get/list
mcp         — list/add/remove
acp         — list/add/remove
rss         — url, limit
loop        — max_iterations
count_tokens— text
send_file   — path, caption
write_file  — path, content
tmux        — start/send/read/list/kill/status (только владелец)
```

Формат вызова модели: `⟪tool:NAME key="val" ...⟫`.
`_maybe_run_tool()` распарсит вызов, уберёт из текста, задиспатчит через `registry.dispatch(name, **args)` и допишет результат.
`_owner_id` — зарезервированный kwarg для owner-гейта (модель не может его подменить; спуфинг исключён).

### Как добавить новый тул
1. Создай `src/antigona/tools/<my_tool>.py` с `async`-хендлером и (опц.) `register(registry)`.
2. Зарегистрируй либо в `_BUILTIN_TOOLS` (`registry.py`, тогда доступен всем runtime'ам через `register_builtins()`),
   либо авто-дискавери через top-level `register()`.
3. Добавь описание в `_INTEGRATION_TOOLS` (`dialogue_engine.py`), чтобы модель могла его вызвать.
4. Напиши unit-тест (паттерн `tests/unit/test_*`).

---

## 7. State-файл CLI

`CLIStateStore` (в `cli.py`) хранит историю flow и last_seq в JSON.
Путь: `--state-file` → `ANTIGONA_STATE_FILE` → `paths.cli_state_file()`.
Структура: `{"flows": {flow_id: {...}}, "last_seq": N}`.
Запись атомарная (tmp + replace). Используется `run`/`attach` для привязки WS-стрима к flow.

---

## 8. Как это тестировать

```bash
cd /var/lib/antigona
# unit (CLI-слой)
.venv/bin/pytest tests/unit/test_cli_ui_layout/ -q
.venv/bin/pytest tests/unit/test_cli_command_parsing.py tests/unit/test_cli_chat_*.py tests/unit/test_approval_picker.py tests/unit/test_command_menu.py -q
# integration (gateway sync / ws reconnect / hitl e2e)
.venv/bin/pytest tests/integration/test_cli_gateway_sync.py tests/integration/test_cli_ws_reconnect.py tests/integration/test_hitl_approval_e2e.py -q
# линт (CI-строгий — mypy --strict src/antigona ОБЯЗАТЕЛЕН)
ruff check src tests
.venv/bin/mypy --strict src/antigona
```

Правило: **не объявлять CLI готовым без живого smoke** — запустить `.venv/bin/antigona chat`
в PTY и увидеть экран глазами, проверить реальный ответ (например «17+28 → 45»), а не эхо-повтор.

---

---

## 6.1 Детальный каталог тул: работа, параметры, результат

> Поведение каждого обработчика снято с реальных тел функций в
> `src/antigona/tools/registry.py` и `src/antigona/tools/tmux_session.py`.
> Формат вызова модели: `⟪tool:NAME key="val" ...⟫`.

### write_file
**Что делает:** создаёт/перезаписывает файл на диске (создаёт родительские каталоги).
- Параметры: `path` (обязательный), `content`.
- Что возвращает: `{"success": true, "path", "bytes"}` — при успехе; `{"error"}` при исключении.
- Комментарий: путь берётся как есть; родительские dirs создаются автоматически.

### send_file
**Что делает:** реально отправляет файл в Telegram через `TelegramAdapter` (не только валидирует).
- Параметры: `path`, `caption=""`, `chat_id=""`.
- Разрешение: авторизует вызывающий (owner ID в Telegram / PIN в CLI). Секретные расширения (`.pem/.key/.p12/.pfx/.crt/.env/.jks`) — блок + флаг `requires_confirmation: true` (CRITICAL-тир).
- Если `path` не найден — ищет относительно `project_root()`.
- Чат/токен: `ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` и `ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID`; мок-режим `ANTIGONA_DELIVERY_MOCK=1`.
- Результат: `{"success", "path", "size", ...adapter}` или `{"error"}`.

### run_shell
**Что делает:** исполняет shell-команду, таймаут 30с.
- Параметры: `command`.
- Блок-лист (substring, lower-case): `shutdown`, `reboot`, `rm -rf /`, `mkfs`, `dd if=/dev/zero`, `passwd`, `iptables -F`, `systemctl` → `{"error": "Command blocked"}`.
- non-zero exit → `{"error": stderr | "Exit code N"}`; timeout → `{"error": "Command timed out after 30s"}`.
- Успех → `{"success": true, "output": "<до 2000 симв.>"}`.

### generate_image
**Что делает:** генерирует изображение по промпту и отправляет.
- Параметры: `prompt`.
- Использует `ImageGenerator` (`image_gen.py`); пустой prompt → error.
- Результат: `{"success": true, "path", "prompt"}` (промпт обрезается до 80 симв.).

### configure_key
**Что делает:** настраивает API-ключ провайдера (полный keyflow).
- Параметры: `provider`, `key`.
- Использует `configure_full_keyflow` (`key_manager.py`).
- Результат: `{"success": true, "provider"}` или `{"error"}`.

### memorize
**Что делает:** записывает факт в файловую память.
- Параметры: `store` (memory|user|…), `content`, `title=""`.
- Лимит: `memory` — 2200 симв., остальные — 1375 симв.; при переполнении → error.
- Результат: `{"success": true, "store", "title"}`.

### count_tokens
**Что делает:** считает токены текста.
- Параметры: `text`.
- Результат: `{"success": true, "tokens": N, "text": <первые 80 симв.>}`.

### kanban
**Что делает:** управление канбан-доской.
- Параметры: `action` (list|create|move|done|get), `title`, `card_id`, `column="in-progress"`, `board`, `body`.
- Доска по умолчанию: `/var/lib/antigona/workspace/.kanban`.
- `create` → новая карточка (возвращает `id`, ставит в `todo`); `move` → `{"success": bool, "id"}`; `done` → закрыть; `get` → карточка; `list` → `{"count", "cards"}`.

### loop
**Что делает:** прогоняет цикл с проверкой (по умолчанию шаги/судья тривиальные в текущей регистрации).
- Параметры: `max_iterations` (default 10, берёт `core.loop.MAX_ITERATIONS_DEFAULT`).
- Результат: `{"success", "status", "iterations", "verdict"}`.

### rss
**Что делает:** забирает новости из RSS-ленты.
- Параметры: `url`, `limit=5`.
- Результат: `{"success", "count", "items": [to_dict()]}`.

### mcp
**Что делает:** управление MCP-серверами.
- Параметры: `action` (list|add|remove), `server`, `kind` (stdio|http), `url`, `command`, `args`.
- `add` + command → `reg.add_stdio`; `add` + url → `reg.add_http`; `remove` → удалить; `list` → имена серверов.

### acp
**Что делает:** управление ACP-агентами.
- Параметры: `action` (list|add|remove), `agent`, `base_url`.
- `add` + base_url → регистрация; `remove` → удаление; `list` → имена агентов.

### tmux (owner-gated)
**Что делает:** тихие фоновые tmux-сессии.
- Параметры: `action` (start|send|read|list|kill|status), `session`, `command`, `keys`, `lines=40`, `cwd`.
- **Жёсткий owner-гейт:** `_owner_id` должен совпасть с `ANTIGONA_OWNER_ID`; иначе отказ БЕЗ пути к запросу доступа (нет approval-пути).
- Имя сессии санитизируется (без `:` `/`, не стартует с `.`); `_blocked()` фильтрует опасные команды.
- `start` требует `command` (запуск в detached-сессии); `send` добавляет `Enter`; `read` читает N строк (default 40); `kill` — `{"killed": true}`.
- Все команды идут без shell (`_run_tmux(args)`), с таймаутом.
- Регистрируется отдельно: `tmux_session.register(registry)` внутри `register_builtins()`.

---

## 6.2 Детальный каталог CLI-команд: что выполняют

### Интерактивный чат
- `antigona chat` — живые диалог с Gateway. Свободный текст → один вызов `/api/v1/dialogue/turn`; `response_type` решает рендер (ответ / live-wait flow / control). Слэш-команды — явный фолбэк. Fail-closed при недоступности Gateway. Опции: `--gateway`, `--token`, `--no-anim`, `-s/--session-id`.

### Запуск и управление задачами (flow)
- `antigona run -g "<goal>"` — создать задачу на Gateway и (опц.) приаттачить live WS-стрим (`--attach`). Тула по умолчанию `workspace.write_text`, путь по умолчанию `task_output.txt`.
- `antigona attach` — приаттачить live WS-стрим к существующему flow.
- `antigona cancel <flow_id>` — отменить flow.
- `antigona approve <flow_id> --yes|--no|--deny` — одобрить/отклонить решение на подтверждении.
- `antigona replay <id>` — хронология переходов: `--timeline` (плоский), `--json` (сырой, для pipe `> replay.json`), фильтры `--actor`, `--entity-type`, `--from`, `--to`.

### Скиллы
- `validate-skill`, `list-skills`, `show-skill`, `promote-skill -r <rev>`, `capture-skill [--state-root]`, `deprecate-skill`.

### Cron
- `cron-create -n <name> -c "<5-польный cron>" -g "<goal>"` + опц. `-p/--path`, `-t/--tool`, `-c/--content`.
- `cron-list`, `cron-show`, `cron-jobs`, `cron-cancel`, `cron-tick`.

### Система / прочее
- `db-upgrade [--db-url]` — миграции БД (alembic/aiosqlite).
- `tui [--refresh]` — TUI-дашборд по Gateway.
- `panel [--refresh, -s/--session-id]` — Rich-панель (до full-screen layout).

### Общие опции подключения
Почти все команды принимают `--gateway` (URL) и `--token` (Bearer). `--token` может быть виден в shell history и argv — соответствующее предупреждение в справке.

---

## 6.3 Контракт тула (legacy ToolABC)

Каждый тул может быть объявлен через `contracts.py` `Tool` (ABC):
- `spec()` → `ToolSpec` (name, description, risk_level: LOW/MEDIUM/HIGH/CRITICAL, input_schema).
- `validate(inp) -> list[str]` — проверка входов до исполнения (список ошибок).
- `execute(inp) -> ToolOutput` — исполнение.

## 9. Памятка разработчика

- CLI = тонкий клиент; вся логика, классификация, память и тулы — на сервере.
- Не добавляй локальный движок/роутер в CLI — нарушит single-core контракт.
- Ошибки Gateway: 401 (токен), 409 (idempotency), 422-499 (validation), 500+ (server) — детали в консоль **redact'ятся** (безопасность), полный traceback — в stderr.
- `resource_id` и всё, что идёт в URL path — строгая валидация.
- Новый тул → registry + `_INTEGRATION_TOOLS` + unit-тест; owner-гейт через `_owner_id`.
- Git-дисциплина: в `/var/lib/antigona` — общее дерево, НЕ пушить в чужие ветки, работать в worktree, пушить только в `hermes/*`.
