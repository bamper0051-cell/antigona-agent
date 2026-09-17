# Фактический контракт Gateway — Этап 1

Источник истины: `src/antigona/main.py`, `schemas.py`, `config.py` в working tree на 2026-07-22. Базовый URL по умолчанию: `http://127.0.0.1:8090`. OpenAPI генерируется FastAPI, но live-сервис в аудите не запускался.

## Общие правила

- Все task endpoints, кроме health, требуют `Authorization: Bearer <token>`.
- Token→owner mapping берётся из `ANTIGONA_DEV_TOKENS` в формате `token:owner,...`; токены сравниваются по SHA-256 digest constant-time.
- Доступ к задаче owner-scoped: чужая задача возвращает 404.
- Создание требует непустой `Idempotency-Key`; ключ уникален на owner. Повтор с тем же payload возвращает 200, другой payload — 409.
- Ошибки FastAPI имеют стандартное тело `{"detail": ...}`.
- Correlation ID клиентом не принимается и в HTTP response отдельно не возвращается.

## Endpoints

### `GET /health`

Auth: нет. Response 200:

```json
{"status":"ok","sandbox":"docker"}
```

Проверяет только построение ответа; не проверяет DB, queue, worker, Docker daemon, verifier или Telegram. Значение sandbox — конфигурация, не runtime probe.

### `POST /tasks`

Headers: bearer auth, `Idempotency-Key`. Body:

```json
{
  "goal": "непустая строка",
  "path": "workspace-relative path",
  "content": "ожидаемое UTF-8 содержимое",
  "tool_name": "workspace.write_text | sandbox.shell",
  "command": ["argv", "без host shell"]
}
```

`command` обязателен и непуст для `sandbox.shell`. Absolute path, `.`/`..` и symlink parent отклоняются. Response: 201 при создании, 200 при idempotent replay, `TaskView`. Task сразу durable-enqueued; Gateway tool не исполняет.

### `GET /tasks/{task_id}`

Response 200: `TaskView`; 401 без/с неверным token; 404 для отсутствующей/чужой задачи.

### `POST /tasks/{task_id}/run`

Повторно enqueue существующего unique queue job, если его состояние допускает. Response `TaskView`. Это **не resume API**: terminal/cancelled задача не оживает; отдельного PAUSED нет.

### `POST /tasks/{task_id}/cancel`

Ставит `cancellation_requested=true`, отменяет pending/running steps, переводит task в `CANCELLED`, журналирует и сохраняет. Terminal task возвращается без изменения. Response `TaskView`.

### `POST /tasks/{task_id}/approvals/{approval_id}`

Body: `{"approve": true|false}`. Approval должен принадлежать загруженной owner task и иметь `PENDING`; иначе 409. Решение сохраняет owner как `decided_by`, затем re-enqueue. Response `ApprovalView`.

### Verifier service: `POST http://127.0.0.1:8091/verify`

Это внутренний отдельный сервис, **не публичный Gateway endpoint**. Требует отдельный verifier bearer credential. Body: `task_id`, `correlation_id`. Только он выполняет CAS `VERIFYING -> DONE` после read-back/hash/content проверки артефакта. Возможные подтверждённые ответы: `{"decision":"DONE"}` или `{"decision":"REPLAN"}`; 401/404/409 для нарушений.

## `TaskView`

Содержит task `id`, `goal`, `target_path`, `status`, `revision`, `checkpoint`, `cancellation_requested`, timestamps и вложенные `steps`, `transitions`, `artifacts`, `approvals`. Не содержит owner, queue position, progress percent, budget, model, worker, runtime, delivery status или verifier details.

## Отсутствующие контракты (`BACKEND_UNAVAILABLE`)

- task list/filter/pagination и queue summary;
- pending approval list, expiration, temporary approval;
- pause/resume/replan/verify-now;
- artifact download/export;
- events/WebSocket/SSE и Telegram session routing;
- goals, agents/subagents, tool registry, memory, skills;
- health dependencies, metrics, logs, audit query;
- settings/config mutation/rollback;
- global emergency stop;
- model/LLM APIs (P0 planner deterministic).

## Persistence/queue/runtime границы

SQLite tables: `task_flows`, `flow_steps`, `state_transitions`, `artifacts`, `approvals`, `queue_jobs`, `durable_operations`, `delivery_outbox`, `schema_version`. Queue claim использует lease/heartbeat и unique job per task. Worker и verifier — отдельные entrypoints. SQLite не предоставляет role-level boundary: строгая защита DONE требует OS ACL или PostgreSQL roles. Docker sandbox default fail-closed по конфигурации; live Docker availability этим аудитом не подтверждена.
