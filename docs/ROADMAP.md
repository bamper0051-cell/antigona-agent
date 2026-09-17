# Antigona — Роадмап разработки

> Автономный ИИ-агент, построенный как «обвязка вокруг чужого ядра» (OpenHands Software Agent SDK).
> Цель — функциональный клон Hermes Agent с Telegram-каналом как первым интерфейсом.
> Clean-room: никакого копирования кода/UI/промптов/skills upstream (Hermes/OpenClaw/Qwen/klio-tech AGPL) — только публичные идеи. См. `AGENTS.md`, `docs/THIRD_PARTY_STRATEGY.md`.

## TL;DR

- **Цель:** агент отвечает в Telegram, исполняет тулы в fail-closed sandbox, детерминированный Verifier единолично ставит DONE.
- **Текущий статус:** ~6% готовности. Архитектура 3 процессов зафиксирована (Gateway / Worker / Verifier), durable-схема в SQLite, Docker-sandbox fail-closed, systemd-юниты и E2E-скрипт на 3 PID есть.
- **До Telegram-готовности (конец P0):** ~3–4 недели остатка.
- **До полного клона Hermes (конец P5):** ~24–33 недели накопительно.

---

## Статус на старт (baseline)

Сделано (см. `docs/ARCHITECTURE.md`):

- **3 процесса:** `antigona-gateway` (auth/approvals/cancel/enqueue, БЕЗ verifier-креда, не исполняет), `antigona-worker` (claims `queue_jobs`, renew leases, Docker-тулы, запрашивает верификацию по HTTP), `antigona-verifier` (отдельный FastAPI, единственный владелец перехода VERIFYING→DONE через bearer + compare-and-set).
- **Durable-таблицы:** `task_flows`, `flow_steps`, `state_transitions`, `artifacts`, `approvals`, `queue_jobs`, `durable_operations`, `delivery_outbox` (SQLite; Postgres+Redis — позже).
- **Sandbox:** Docker fail-closed — no-net, read-only root, cap-drop, no-new-privileges, non-root UID, CPU/RAM/PID-лимиты, tmpfs, timeout, монтируется только 0750 workspace.
- **Доставка:** DeliveryAdapter отвязывает прогресс от канала; TelegramAdapter send-only; Gateway — единственный владелец канала.
- **Инфра:** systemd-юниты для трёх сервисов, E2E-скрипт на 3 PID.
- **Правила:** состояние — только через state machine; тулы P0 не ходят в сеть/шелл/вне workspace; sticky cancel; ruff/mypy/pytest.

**Оценка готовности: ~6%** (скелет процессов и схема есть; ядра агента, Verifier-логики, Telegram, HITL — нет).

---

## P0 — Ядро + Telegram (8 шагов, ~3–4 недели остатка)

Ядро: **OpenHands SDK** (`openhands-sdk` + `openhands-tools`, MIT), in-process `LocalConversation` внутри Worker.

> ⚠️ **Развилка Python (решить в Спринте 1, шаг 2):** репо требует Python 3.11+ (AGENTS.md), но OpenHands SDK жёстко требует **Python 3.12** (issue #1363: «Currently we only support Python 3.12»); CLI upstream отстаёт (1.21.0 vs SDK 1.36.1). Решение по умолчанию: **отдельный venv 3.12 для Worker** (интеграция SDK), остальные процессы могут остаться на 3.11; альтернатива — pin SDK-версии, совместимой с 3.11 (проверить фактически). Решение зафиксировать в `docs/DECISIONS.md` (ADR-0001).

### Спринт 1 (неделя 1): Gateway API + ядро агента

#### Шаг 1. Gateway FastAPI: REST + WS + correlation_id
- **Цель:** единая точка входа: создание task_flow, подписка на прогресс, cancel, approvals.
- **Что сделать:**
  - REST: `POST /flows`, `GET /flows/{id}`, `POST /flows/{id}/cancel`, `POST /approvals/{id}/decision`.
  - WS/SSE-канал прогресса на flow; каждый запрос/событие несёт `correlation_id` (генерируется на входе, протаскивается в `state_transitions` и логи).
  - Gateway только enqueue'ит в `queue_jobs` через state machine — не исполняет.
- **Чекпоинт:** `pytest tests/gateway/` зелёный; `curl POST /flows` → строка в `task_flows` + `queue_jobs` в SQLite; WS отдаёт события с тем же `correlation_id`; попытка Gateway выставить DONE невозможна по коду (нет verifier-креда).
- **Артефакт:** `antigona/gateway/api.py`, OpenAPI-схема (`/openapi.json`), тесты.

#### Шаг 2. OpenHands SDK: LocalConversation + 3–5 тулов
- **Цель:** Worker запускает агента in-process и исполняет собственные clean-room тулы.
- **Что сделать:**
  - venv 3.12, pin `openhands-sdk` / `openhands-tools`; зафиксировать ADR-0001 (см. развилку выше).
  - `LocalConversation` с `persistence_dir` + `conversation_id` (персистентность разговора между рестартами Worker).
  - Свои тулы через `register_tool`: shell (только внутри sandbox-workspace), file read/write (только workspace), web-fetch (в P0 — заглушка/выключен: тулы P0 не ходят в сеть; включается в P4 через egress-proxy).
  - Streaming — через `callbacks=[]` (НЕ `event_stream.subscribe` — не работает в V1). События конвертируются в `state_transitions`/outbox.
  - **Запрет:** не дёргать `execute_tool()` напрямую для недоверенного ввода — обходит SecurityAnalyzer и policy.
- **Чекпоинт:** интеграционный тест: flow «создай файл X в workspace» проходит SDK-циклом, файл появляется на диске в 0750 workspace; рестарт Worker посреди разговора → разговор восстанавливается из `persistence_dir`.
- **Артефакт:** `antigona/worker/agent_core.py`, `antigona/worker/tools/*.py`, ADR-0001, тесты.

### Спринт 2 (неделя 2): Durable + Sandbox

#### Шаг 3. Durable state-machine + recovery worker + sticky cancel (SQLite)
- **Цель:** ни один переход состояния мимо state machine; упавший Worker не теряет задачу.
- **Что сделать:**
  - Формализовать переходы (enum + таблица допустимых переходов), optimistic lock по `revision`, все записи через `state_transitions`.
  - Claim из `queue_jobs` с lease + heartbeat renew; recovery worker возвращает протухшие lease в очередь.
  - Sticky cancel: флаг отменённого flow переживает рестарты, Worker проверяет его перед каждым шагом и убивает контейнер тула.
  - Outbox-паттерн для `delivery_outbox` (exactly-once доставка).
- **Чекпоинт:** тест «kill -9 Worker посреди шага» → recovery worker переотдаёт job, flow доезжает; тест «cancel + рестарт» → flow остаётся CANCELLED, шаги не исполняются; недопустимый переход → исключение и запись отказа.
- **Артефакт:** `antigona/durable/state_machine.py`, `antigona/durable/recovery.py`, набор chaos-тестов.

#### Шаг 4. Sandbox: gVisor поверх Docker
- **Цель:** усилить существующий fail-closed Docker вторым слоем изоляции ядра.
- **Что сделать:**
  - Установить `runsc`, добавить runtime `--runtime=runsc` для контейнеров тулов (rootless Docker где возможно).
  - Сохранить весь текущий fail-closed профиль: deny-by-default сеть (`--network none`), лимиты CPU/RAM/PID, timeout, read-only root, non-root UID.
  - Никогда не пробрасывать `docker.sock` внутрь; спавн — только с хост-стороны Worker (Sysbox/сокет-брокер — тема P4).
  - Fallback-флаг конфигурации на runc, если runsc недоступен на хосте (с громким WARN в лог).
- **Чекпоинт:** `dmesg`/`runsc` подтверждает исполнение под gVisor; тест: тул пытается выйти в сеть → fail; тул пытается писать вне workspace → fail; fork-бомба гасится PID-лимитом.
- **Артефакт:** `antigona/sandbox/runner.py` (runsc-профиль), `docs/SANDBOX.md`, тесты-«побеги».

### Спринт 3 (неделя 3): Verifier + HITL

#### Шаг 5. Verifier v1 (детерминированный, владеет DONE)
- **Цель:** DONE ставится только Verifier'ом по проверяемым критериям.
- **Что сделать:**
  - Проверки: файл существует/хеш совпадает, тест-команда проходит, endpoint отвечает. Verifier исполняется ВНЕ sandbox агента; критерии агенту не видны.
  - Модель/Worker шлёт `REQUEST_COMPLETION` (никогда не финализирует сама) → Gateway/Worker дергает Verifier по HTTP с bearer; Verifier делает единственный `UPDATE VERIFYING→DONE` через compare-and-set по `revision`.
  - HTTP 401 без креда; кред есть только у Verifier-процесса.
- **Чекпоинт:** тест: запрос без bearer → 401; попытка Worker выставить DONE напрямую → отвергнута state machine; flow с невыполненным критерием → VERIFY_FAILED, не DONE; двойной cAS → второй проигрывает.
- **Артефакт:** `antigona/verifier/app.py`, `antigona/verifier/checks/*.py`, тесты.

#### Шаг 6. Human-in-the-loop approvals
- **Цель:** рискованные действия требуют явного подтверждения человека.
- **Что сделать:**
  - `set_confirmation_policy` + `SecurityAnalyzer` из SDK: риски LOW/MEDIUM/HIGH; LOW — авто, MEDIUM/HIGH — pause + запись в `approvals`.
  - **Обязательно подключить SecurityAnalyzer**: без него `security_risk` от LLM даёт `RuntimeError` (issue #11309).
  - Gateway REST/WS отдаёт pending approvals; решение → resume/reject flow через state machine; timeout approval → auto-reject.
- **Чекпоинт:** тест: HIGH-действие ставит flow в WAITING_APPROVAL; reject → шаг не исполняется; approve → исполняется; рестарт Worker в ожидании approval ничего не ломает.
- **Артефакт:** `antigona/worker/hitl.py`, политика рисков в `docs/RISK_POLICY.md`, тесты.

### Спринт 4 (неделя 4): Каналы + наблюдаемость

#### Шаг 7. Telegram-бот + CLI (параллельно)
- **Цель:** первый пользовательский канал и dev-клиент.
- **Что сделать (Telegram, aiogram 3):**
  - Паттерн «одна карточка + edit»: прогресс flow — через `editMessageText` с throttle, без спама.
  - Approvals — inline-кнопки; `callback_data` ≤64 байт через CallbackData factory.
  - FSM (на старте — memory/SQLite, Redis — при миграции P4); доставка через `delivery_outbox` (exactly-once); длинные логи — файлом-вложением. TelegramAdapter остаётся send-only, Gateway — единственный владелец канала.
- **Что сделать (CLI):** Typer + Rich, тонкий клиент Gateway (REST + WS/SSE): `antigona run`, `antigona attach <flow>` (живой стрим), `antigona cancel`, `antigona approve`. (Textual TUI — P2.)
- **Чекпоинт:** живой сценарий: задача из Telegram → карточка редактируется по ходу → HIGH-действие → кнопки approve/reject работают → DONE от Verifier отражён в карточке; `antigona attach` показывает тот же стрим; рестарт бота не дублирует сообщения (outbox).
- **Артефакт:** `antigona/channels/telegram/bot.py`, `antigona/cli/main.py`, ручной чек-лист `README.md`.

#### Шаг 8. Memory / observability (минимум P0)
- **Цель:** сквозная трассируемость и базовая память сессии.
- **Что сделать:** structured-логи (JSON) с `correlation_id` во всех трёх процессах; immutable audit-trail на базе `state_transitions`; trust-tagging выходов тулов (метка untrusted в событиях); хранение summary разговора рядом с `persistence_dir` (векторная память — P1).
- **Чекпоинт:** по одному `correlation_id` grep'ом собирается полная траектория flow через логи всех трёх процессов; audit-записи append-only (нет UPDATE/DELETE в коде).
- **Артефакт:** `antigona/observability/logging.py`, `docs/OBSERVABILITY.md`.

**🏁 Артефакт конца P0:** агент принимает задачу в Telegram, исполняет тулы в gVisor-sandbox, спорные шаги идут через approval, Verifier детерминированно ставит DONE, всё восстановимо после падений. E2E-скрипт на 3 PID зелёный.

---

## P1 — Память + умный Verifier (~3–4 нед)

- Векторная память + profile-память (pgvector; влечёт поднятие Postgres хотя бы для памяти — оценить раннюю миграцию durable, см. «Риски»).
- Verifier v2: LLM-судья на **второй модели** (не той, что агент) + trajectory-мониторинг — защита от reward-hacking; критерии по-прежнему скрыты от агента.
- `artifacts` с контент-хешами: верификация «файл создан» → «файл создан и не подменён».
- Расширение trust-tagging: деградация прав тулов после чтения untrusted-контента (OWASP Agentic Top 10).

## P2 — Skills + Cron + Replay + TUI (~4–5 нед)

- Skills registry (clean-room, свой формат) + самообучение: сохранение удачных траекторий как skills.
- Cron-планировщик поверх `queue_jobs` (durable расписания, sticky cancel применим).
- Replay-UI: воспроизведение flow из `state_transitions`/persistence для отладки.
- Textual TUI поверх существующего CLI-клиента Gateway.

## P3 — Subagents + бэкенды (~5–7 нед)

- Subagents / параллельные воркстримы: дочерние task_flows, агрегация результатов, лимиты глубины/бюджета.
- Дополнительные execution-бэкенды: SSH, Modal, Daytona (через workspace-абстракцию SDK: LocalWorkspace→DockerWorkspace без правки кода агента).
- Dual-LLM-идея для недоверенного контента (quarantine-модель без тулов; не полный CaMeL — он 0% на open-ended).

## P4 — Изоляция + масштаб (~5–7 нед)

- Firecracker/micro-VM или E2B для тулов высокого риска; спавн контейнеров через Sysbox/сокет-брокер (никогда docker.sock).
- Миграция SQLite → **Postgres** (outbox + `SKIP LOCKED` + revision/optimistic lock + heartbeat recovery) + Redis (FSM Telegram, кэш).
- Durable: остаёмся на самописной state-machine; запасной вариант — **DBOS** (живёт в том же PG, ~+7 строк); Temporal — только по триггерам (см. «Риски»).
- Smart egress-proxy c allowlist → включение сетевых тулов (web-fetch по-настоящему).

## P5 — Полный клон Hermes (+4–6 нед)

- Каналы: Discord, Slack, WhatsApp, Signal, Email — как DeliveryAdapter'ы поверх того же Gateway/outbox.
- Остальные бэкенды: Singularity, Daytona (полная матрица).
- Web UI (тонкий клиент Gateway REST+WS, replay + approvals + артефакты).

---

## Итоговая таблица

| Фаза | Шаги | Недели | Накопительно | Что закрывает |
|------|------|--------|--------------|----------------|
| P0 | 8 (4 спринта) | 3–4 | 3–4 | Ядро SDK, durable, sandbox gVisor, Verifier v1, HITL, Telegram+CLI, observability |
| P1 | память, Verifier v2 | 3–4 | 6–8 | pgvector-память, LLM-судья, anti-reward-hacking, artifacts-хеши |
| P2 | skills, cron, replay, TUI | 4–5 | 10–13 | Самообучение, расписания, отладка, TUI |
| P3 | subagents, бэкенды, dual-LLM | 5–7 | 15–20 | Параллелизм, SSH/Modal/Daytona, защита от инъекций |
| P4 | micro-VM, PG/Redis, egress | 5–7 | 20–27 | Изоляция уровня VM, масштаб, сетевые тулы |
| P5 | каналы, Web UI | 4–6 | 24–33 | Полный функциональный клон Hermes |

---

## Риски и развилки

1. **Python 3.11 ↔ 3.12 (блокер шага 2).** SDK требует 3.12; репо — 3.11+. По умолчанию: venv 3.12 для Worker, зафиксировать в ADR-0001 и поправить AGENTS.md; альтернатива — pin совместимой SDK-версии (проверить фактом, не верить на слово). CLI upstream (1.21.0) отстаёт от SDK (1.36.1) — CLI upstream не используем, свой клиент.
2. **SQLite → Postgres.** Триггеры ранней миграции (раньше P4): нужен pgvector в P1; конкуренция нескольких Worker'ов упирается в блокировки SQLite; потребность в `SKIP LOCKED`. Схема с самого начала пишется миграциями (alembic), чтобы переезд был механическим.
3. **Когда Temporal/DBOS.** Не Temporal на старте. Триггеры: рост числа долгоживущих флоу и compensations, боль в ручных recovery-сценариях, мультирегион. Первый шаг эскалации — DBOS в том же PG; Temporal — только если DBOS не хватает.
4. **Reward-hacking.** Пока Verifier только детерминированный (P0) — риск гейминга критериев низкий, но LLM-судья P1 обязан быть второй моделью, критерии скрыты, trajectory-мониторинг включён; иначе агент оптимизирует под судью.
5. **SDK-грабли (фиксируем как инварианты):** streaming только через `callbacks=[]`; `SecurityAnalyzer` обязателен (иначе RuntimeError на `security_risk`, issue #11309); `execute_tool()` не для недоверенного ввода; персистентность только через `persistence_dir`+`conversation_id`.
6. **Clean-room дисциплина.** Любое заимствование сверх публичных идей — только после аудита лицензий (`docs/THIRD_PARTY_STRATEGY.md`); AGPL-код (klio-tech) не читать при написании аналогов.
