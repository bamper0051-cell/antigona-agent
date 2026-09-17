# Архитектура Antigona — для внешнего читателя

Этот документ объясняет устройство проекта простыми словами: из каких слоёв он
состоит, как проходит задача и **где именно стоят тормоза**. Он не заменяет
код — все утверждения проверяемы по указанным файлам.

> **Статус:** проект в разработке. См. [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md) —
> это единственный операторский документ, который определяет, что считается
> доказанным. Если что-то в этом файле расходится с кодом — прав код.

---

## 1. Что это такое в одном абзаце

Antigona — это **durable agent harness**: среда, в которой автономный агент
принимает задачу, планирует шаги, вызывает инструменты, проверяет результат по
факту на диске и доставляет ответ. Ключевое слово — *durable*: состояние задачи
живёт в базе данных, а не в памяти процесса, поэтому задача переживает
перезапуск воркера. Ключевое свойство — *fail-closed*: когда что-то неизвестно
(нет документа, непонятен путь, нет доказательства), система отказывает, а не
делает предположение.

---

## 2. Три слоя

```
┌──────────────────────────────────────────────────────────────────────┐
│ СЛОЙ 1 — КАНАЛЫ (как с системой говорят)                             │
│   CLI (typer)          TUI/Console (textual)      Telegram-бот       │
│   antigona             antigona-tui                antigona-bot       │
│   src/antigona/cli.py  src/antigona/tui.py         src/antigona/channels/telegram/
└───────────────────────────────┬──────────────────────────────────────┘
                                │  InputPipeline: нормализация, подпись
                                │  входа, классификация риска, санитайз
┌───────────────────────────────▼──────────────────────────────────────┐
│ СЛОЙ 2 — ЯДРО РЕШЕНИЙ (что делать)                                   │
│   Router / IntentRouter → Conversation / Planner → Policy            │
│   src/antigona/router/ · conversation/ · planner/ · policy/          │
│   Единый chokepoint исполнения инструментов: engine/unified_executor │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  задача превращается в durable TaskFlow
┌───────────────────────────────▼──────────────────────────────────────┐
│ СЛОЙ 3 — TASK RUNTIME (как выполняется, переживая сбои)              │
│   Gateway (FastAPI) → очередь → Worker → Verifier → Delivery         │
│   src/antigona/gateway/ · worker/ · verifier/ · delivery/            │
│   Хранилище: SQLite (по умолчанию) или PostgreSQL; migrations/*.sql  │
│   Исполнение shell/кода: Docker-песочница (no-net, read-only, cap-drop)
└──────────────────────────────────────────────────────────────────────┘
```

Точки входа зарегистрированы в `pyproject.toml` (`[project.scripts]`):
`antigona`, `antigona-cli`, `antigona-gateway`, `antigona-worker`,
`antigona-verifier`, `antigona-delivery`, `antigona-tui`, `antigona-tui-control`,
`antigona-console`, `antigona-bot`, `antigona-api`.

---

## 3. Путь одной задачи

1. **Приём.** Канал (CLI/Telegram/TUI) отдаёт ввод в `input_pipeline`.
   Ввод нормализуется и проходит проверку безопасности **до** любой записи в БД.
2. **Постановка.** Задача попадает в Gateway. Он проверяет права, присваивает
   `idempotency_key` и создаёт durable-запись `TaskFlow`
   (`src/antigona/models.py`). Статус начинается с `RECEIVED`.
3. **Планирование.** Планировщик разбивает цель на шаги (`FlowStep`); для
   read-only задач есть отдельный пошаговый контур `turn_bridge`.
4. **Политика.** `policy/` и `security/risk_classifier.py` решают, что можно
   делать молча, что требует одобрения владельца, а что запрещено.
5. **Исполнение.** Worker берёт задачу из очереди (lease, heartbeat) и вызывает
   инструмент **через единственный chokepoint** — `engine/unified_executor.py`.
   Опасные операции уходят в Docker-песочницу.
6. **Наблюдение.** Результат шага фиксируется наблюдателем (`durable/observer.py`)
   вместе с признаками выполнения; всё пишется в журнал переходов.
7. **Верификация.** Только сервис Verifier переводит задачу `VERIFYING → DONE`,
   сравнивая артефакт на диске (SHA-256) с ожиданием. См. `verifier_service.py`,
   `durable/verifier.py`.
8. **Доставка.** Готовый результат уходит в канал доставки; отдельный воркер
   отвечает за receipts и повторы.

**Состояния задачи** (20 значений, `TaskState` в `src/antigona/models.py`):
`CREATED`, `RECEIVED`, `QUEUED`, `READY`, `PLANNING`, `RUNNING`, `TOOL_EXECUTING`,
`OBSERVING`, `VERIFYING`, `WAITING_APPROVAL`, `WAITING_USER`, `PAUSED`,
`RETRY_SCHEDULED`, `REPLAN_REQUESTED`, `DONE`, `FAILED`, `BLOCKED`, `CANCELLED`,
`TIMEOUT`, `POLICY_DENIED`. Каждый переход журналируется.

---

## 4. Где стоят тормоза (гейты)

Это самое важное в архитектуре. Гейты встроены в путь исполнения, а не
приклеены сбоку.

| # | Гейт | Где | Что именно делает |
|---|------|-----|-------------------|
| G1 | **Санитайз входа** | `input_pipeline/`, `result_safety.py`, `src/antigona/security/_credentials.py` | Секреты и опасные конструкции вычищаются до записи в БД; в отчётах не появляется сырой ввод. |
| G2 | **Классификация риска** | `security/risk_classifier.py`, `policy/` | Разделяет безопасное, требующее одобрения и запрещённое. |
| G3 | **Approval владельца** | `security/approval_grant.py`, `core/owner_gate.py`, `security/owner_identity.py`, `security/totp.py` | MEDIUM+ операции не выполняются без явного одобрения; grant одноразовый. |
| G4 | **Ограда рабочей области** | `path_boundary.py`, `filesystem.py`, `workspace.py`, `shell.py` | Пути проверяются двумя слоями, оба fail-closed: (а) лексически — `..`, абсолютные, Windows/UNC, управляющие символы, encoded-варианты; (б) по идентичности ОС — `st_dev` + `st_ino` вместо сравнения строк, поэтому алиасы (symlink-предки, регистр, Unicode NFC/NFD, bind-монтирования) не пробивают ограду. |
| G5 | **Единый chokepoint инструментов** | `src/antigona/engine/unified_executor.py` | Инструмент нельзя выполнить в обход: мимо него нет путей в `ToolRegistry`-диспетчеризацию. |
| G6 | **Песочница** | `sandbox/docker_sandbox.py`, `sandbox/runner.py` | Для опасных shell/кода: контейнер с `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `read_only`, сеть `none`. Не удалось изолировать — отказ. |
| G7 | **Verifier-only DONE** | `verifier_service.py`, `durable/verifier.py` | Перевод в `DONE` — CAS `VERIFYING → DONE` и только у сервиса-верификатора, у которого есть нужный креденциал. Ни Gateway, ни Worker этого не могут. |
| G8 | **Доказательство, а не отчёт** | `verifier_service.py`, `durable/observer.py`, `core/evidence_registry.py` | Артефакт проверяется на диске (существование, тип, SHA-256, содержимое). Обещание исполнителя «сделано» не считается доказательством. |
| G9 | **Конверт неизменяемости** | `CANDIDATE_DEPLOYMENT_MANIFEST.json` + `src/antigona/startup/validator.py` | Манифест фиксирует содержимое кандидата; несовпадение хеша — отказ (fail-closed), а не предупреждение. Любая правка отслеживаемого файла требует пересчёта манифеста — иначе валидатор останавливает запуск. |

Общий принцип: **неизвестное состояние = отказ**. Ни один гейт не «разрешает по
умолчанию, если что-то не прочиталось».

---

## 5. Что делает задачу переживающей сбои

- **Durable-хранилище.** SQLite по умолчанию, PostgreSQL для продакшена; схема
  развивается только через `migrations/*.sql` (+ Alembic: команда `antigona db`).
- **Идемпотентность.** `idempotency_key` + уникальное ограничение
  `uq_owner_idem` не дают создать дубль одной и той же операции.
- **Lease и heartbeat.** Задачу держит один воркер (`lease_owner`,
  `lease_expires_at`); зависшая работа перевыпускается, а не выполняется дважды.
- **Восстановление.** `durable/recovery.py`, `durable/state_cache.py`,
  `durable/operation_store.py` (CAS, claims, manifest) поднимают прерванную
  задачу после `kill -9` без дублирования побочных эффектов.
- **Журнал и корреляция.** Каждый переход состояния и каждое событие
  журналируются; сквозной `correlation_id` проходит от Gateway до Verifier
  (`core/event_log.py`, `chain/`, `observability.py`).

---

## 6. Статические гейты качества

Те же команды, что и в CI (`.github/workflows/ci.yml`):

```bash
ruff check src/ tests/
python3 scripts/arch_guard.py
mypy --strict src/antigona
ulimit -Sn 1024
unset ANTIGONA_PIN
pytest tests --tb=short -q
```

- **ruff** — стиль и очевидные ошибки (`pyproject.toml` → `[tool.ruff]`).
- **arch_guard** (`scripts/arch_guard.py`, ADR-007) — архитектурная ограда:
  запрещает новые `Path.home()`, `expanduser(`, хардкод корня проекта, чтение
  `config.yaml` и прямые SDK-импорты провайдеров вне адаптеров. Канонический
  определитель корня — единственный файл `src/antigona/core/paths.py`.
  Существующие отклонения перечислены в `scripts/arch_baseline.txt` — этот
  список обязан только сокращаться.
- **mypy --strict** — типизация обязательна, конфиг `[tool.mypy]`.
- **pytest** — тесты запускаются с мягким лимитом дескрипторов (`ulimit -Sn 1024`),
  то есть тест, который течёт по файловым дескрипторам, честно падает.

---

## 7. Карта репозитория

```
src/antigona/
  cli.py, tui.py, main.py        ← CLI, TUI, консоль
  channels/telegram/             ← Telegram-канал
  input_pipeline/, router/, conversation/, planner/, policy/
                                 ← приём, маршрутизация, диалог, план, правила
  engine/unified_executor.py     ← единственный chokepoint исполнения инструментов
  tools/, plugins/, skills/      ← инструменты, плагины, навыки (ASKILL)
  sandbox/                       ← Docker/microVM изоляция
  path_boundary.py, filesystem.py, workspace.py, shell.py
                                 ← ограда рабочей области (лексика + идентичность ОС)
  security/                      ← риск, approvals, TOTP/OTP, аудит, owner identity
  durable/                       ← state machine, observer, recovery, operation store
  gateway/, worker/, verifier/, delivery/
                                 ← сервисы task runtime
  models.py, database.py, repository.py
  core/paths.py                  ← единственный источник правды о путях проекта
  startup/, health/, diagnostics/← валидатор при старте, здоровье, диагностика
migrations/                      ← SQL-схема
tests/                           ← тесты (unit/integration/e2e/security/...)
scripts/                         ← arch_guard, локальный CI, e2e-раннеры
deploy/systemd/                  ← systemd-юниты и установщик юнитов
docs/                            ← RELEASE_RUNBOOK, ADR, безопасность, роадмап
```

---

## 8. Что важно знать, прежде чем что-то менять

1. **Один писатель на кандидата.** Параллельные правки одного файла запрещены
   процессом: ревьюер обязан быть не автором.
2. **Гейты не ослабляют ради зелёного.** Нельзя поднимать `ulimit`, удалять или
   скипать тесты, обходить approval/fence/chokepoint.
3. **Mock и stub допустимы только внутри явно тестовой зоны.** В рабочем
   контуре подмена «зелёным заглушком» — дефект, а не упрощение.
4. **Секреты не попадают в git, отчёты, логи и скриншоты.** Ключи читаются из
   окружения/`.env` (см. `contracts/config.example.yaml`).
5. **Опасные и необратимые операции — только с одобрения владельца.** Автоматического
   одобрения не существует.

Практические шаги для участника — в [`CONTRIBUTING.md`](CONTRIBUTING.md),
порядок сообщения об уязвимости — в [`SECURITY.md`](SECURITY.md).
