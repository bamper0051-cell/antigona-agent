# Текущая архитектура Antigona (read-only карта, 2026-08-03)

Источник: трассировка исходников `/var/lib/antigona` (ветка `unify/one-gateway-one-memory`).
Цель: карта модулей для интеграционной разведки (Этап 0.3 мастер-плана) — вход/выход/зависимости/заменяемость адаптером.

## Точка входа

| Слой | Модуль | Вход | Выход | Заменяемость |
|---|---|---|---|---|
| Gateway | `gateway/api.py` `create_gateway_app` (FastAPI :8090) | HTTP: /flows, /tasks, /api/v1/dialogue/turn, /approvals, /sessions, /api/v1/memory, /commands, WS | TaskView/JSON | канон, сохранить |
| Gateway main | `gateway/main.py` + `gateway/__init__.py::main` | env | uvicorn | канон |
| Core | `core/brain.py` `AntigonaBrain.process` | text, user_id, channel, session_id | BrainResponse (conversation/task_accepted/clarification/control/error) | канон — единственная точка маршрутизации |
| Worker | `worker/__init__.py main()` | DurableQueue.claim | TaskFlow (переходы состояний) | канон |
| Verifier | `verifier_service.py` (:8091) | POST /verify (HMAC) | verdict → CAS VERIFYING→DONE | канон — единственный владелец DONE |
| CLI | `cli.py` + `cli_ui/chat.py` | stdin | GatewayClient | тонкий клиент |
| Telegram | `channels/telegram/bot.py` (~1830 стр.) | aiogram update | send_dialogue_turn + presenter | тонкий клиент |

## Доменные контракты (текущее состояние)

### Tool — 4 параллельных контракта (P0.1)
1. `contracts.py::Tool` (Protocol, Generic[InputT]) — **мёртв**: никто не импортирует.
2. `tools/contracts.py::Tool` (ABC: spec/validate/execute) + ToolSpec/ToolInput/ToolOutput — **канон инструментов ядра**
   (используют `tools/filesystem_read.py`, `tools/execution_service.py`).
3. `tools/registry.py::Tool` (dataclass: name/toolset/schema/handler) + ToolRegistry + register_builtins —
   descriptor-контур; register_builtins вызывается только из LEGACY `api/server.py`.
4. `tools/action_executor.py::Action/ActionType/ActionExecutor` — легаси-«действия»; ActionType используется
   активным security-кодом (`security/risk_classifier.py`, `security/otp.py`) и `tools/web_search.py` (docstring),
   shim в `tools/registry.py`; ActionExecutor — в LEGACY `api/server.py` и `task/runtime.py`.

### EventBus — 2 + аудит (P0.2)
- `events/bus.py` — typed in-memory, канон для транспорта (бот/OperationPresenter).
- `tasks/event_bus.py` — JSONL-дубль; используется только легаси-контуром (`channels/telegram/context_adapter.py`,
  `status_renderer.py`, `tasks/*`), которые активным ботом не импортируются.
- `core/event_log.py` — DB-журнал переходов задач (state_transitions), канон для задач.
- `observability.event()` — audit-лог событий.

### Memory/session — несколько путей (P0.3)
- `core/memory_repository.py` (таблица `memory_entries`) — **канон долгосрочной памяти** (owner-scoped, kinds:
  fact/preference/profile/user/memory). Используется: `/api/v1/memory`, `ContextBuilder`, `DialogueEngine` (MEMORIZE).
- `sessions/repository.py` (antigona_sessions.db: sessions/messages/decisions/task_refs) — **канон истории диалога**.
- `memory/summarizer.py` MemorySummarizer — per-session буфер+резюме (контекст, не хранилище).
- `memory/file_memory.py` FileMemory (.memory/MEMORY.md, USER.md) — fallback в `ContextBuilder`; запись в
  `tools/registry.py::_handle_memorize` (легаси-контур).
- `memory/long_term.py`, `memory/postgres_memory.py`, `memory/self_learning.py` — легаси (SelfLearningTool
  используется только `presentation/presenter.py` при SUCCEEDED-доставке).

## Смежные слои

- **Storage**: SQLAlchemy ORM (`models.py`, SCHEMA_VERSION=7), SQLite/Postgres, миграции (SQL 0001–0007 + Alembic),
  append-only триггеры state_transitions. DurableQueue (queue_jobs, lease/CAS), delivery_outbox.
- **Security/policy**: `security/risk_classifier.py` RiskClassifier (ActionType), `security/otp.py`, `tools/pin_gate.py`
  RiskClass (SAFE/SENSITIVE/CRITICAL) + PIN/OTP, `worker/hitl.py` ConfirmationPolicy (NEVER/HIGH_ONLY/ALWAYS),
  `repository.py` SensitiveTaskInput, `core/task_registry.py` evidence-контракты. — **Permission-логика разрознена**
  (несколько точек классификации риска: RiskClassifier vs pin_gate vs hitl).
- **MCP**: `mcp/` — client (stdio/HTTP/SSE), discovery, execution, registry — есть, работает через свои DTO.
- **Subagents**: `subagents/base.py::SubagentAdapter`, `subagents/registry.py::SubagentRegistry.select(TaskType)`,
  адаптеры claude_code/codex — есть (используются worker'ом для write-задач).
- **Skills/plugins**: `skills/` (lifecycle/registry/store/matcher/capture), `plugins/` (PluginRegistry/loader) — есть.
- **Delivery**: `delivery/` (DeliveryWorker/Router/adapters) поверх delivery_outbox.
- **Replay**: `gateway/api.py` /flows/{id}/replay.

## Контрольные сценарии (Этап 0.4) — текущий статус
1. Обычный вопрос в Telegram → Turn API → conversation ✓ (e2e проверено).
2. Задача write → Worker → approval → Verifier → DONE ✓ (e2e проверено: задача b7fc8f92).
3. Ошибка LLM → verifier fail-closed → FAILED ✓ (проверено).
4. Одобрение через CLI approve ✓; отмена через /flows/{id}/cancel ✓.
5. Перезапуск процесса с сохранением состояния — durable (БД), worker/gateway/verifier независимы.

## Что уже годится (по аудиту)
Единое ядро, канальные адаптеры (частично), durable runtime, Verifier, approvals, EventBus (канон),
Security/approvals, Tool Registry (канон ABC), MCP, SubagentAdapter, skills/plugins.
См. `docs/ROADMAP.md` для сопоставления с целевыми контрактами.
