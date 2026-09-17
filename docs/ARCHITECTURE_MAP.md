# Архитектурная карта Antigona (Single Runtime)

## Единый поток

```
Telegram / CLI / Dashboard
  │
  ▼
InputPipeline (process_user_input)
  │  ├── UserInputEnvelope (source, user_id, chat_id, text, reply_to, edit)
  │  ├── ContextResolver (reply → existing task, edit → revision)
  │  ├── IntentRouter (classify: task.* → Gateway, conversation.* → LLM)
  │  └── TelegramMessageBinding (persistent chat_id↔task_id)
  │
  ├── Task intent → GatewayClient.submit() → POST /flows
  │     └── статус QUEUED (НЕ DONE, НЕ "задача создана")
  │
  └── Conversation → chitchat_reply (LLM, без ActionExecutor)
```

## Operation Lifecycle (progress → final → cleanup)

```
User message
  │
  ├── 1. OperationStore.create() → Operation (state=RECEIVED)
  ├── 2. EventBus → OperationReceived → Presenter → send progress 🎯
  ├── 3. InputPipeline → GatewayClient.submit()
  ├── 4. EventBus → StageChanged → Presenter → edit progress
  ├── 5. Gateway Worker → execute → Observer → Verifier
  ├── 6. EventBus → FinalResponseReady → Presenter.deliver_final()
  │     ├── claim_final_delivery (CAS, at-most-once)
  │     ├── send final ✅ / ❌ (Telegram send_message)
  │     ├── add_final_message_id (manifest persistence)
  │     ├── commit_terminal (operation → SUCCEEDED / FAILED / CANCELLED)
  │     └── cleanup_progress (delete progress message)
  └── 7. Reply/Edit → steer existing operation (CAS, no duplicate)
```

## Gateway Task Flow (20-state single state machine)

```
CREATED → QUEUED → PLANNING → READY → TOOL_EXECUTING → OBSERVING → VERIFYING
              ↕            ↕              ↕                          ⇣⇣⇣⇣⇣⇣
          RETRY_SCHEDULED  REPLAN_REQUESTED  PAUSED             DONE (Verifier only)
              ↕            ↕                                    FAILED / BLOCKED
          WAITING_USER   WAITING_APPROVAL                     TIMEOUT / POLICY_DENIED
                                                                 CANCELLED
```

## Ключевые правила (AGENTS.md)

- `POST /flows` → `201 QUEUED` или `200` idempotent. **Не DONE**.
- Только `Verifier` имеет capability для `VERIFYING → DONE`.
- `WAITING_APPROVAL` — nonterminal.
- `EventBus.publish()` — fan-out, **не** delivery acknowledgement.
- `OperationPresenter.deliver_final()` — единственный владелец Telegram final.
- At-most-once: claim-before-send, manifest-after-send, terminal-commit-after-manifest.
- Security: pre-commit `SensitiveTaskInput` → `422`, SHA-256 аргументов в persistence.
- Sanitizer: credentials, path traversal, HTML-escape, empty evidence → fail-closed.
- Gateway waiter: monotonic deadline, final GET при cancellation, revision agreement.
- ActionExecutor: только `READ_FILE` локально; все write/shell/code — через Gateway.

## Компоненты

| Компонент | Назначение | Статус |
|-----------|-----------|--------|
| `OperationStore` | Durable async repository (CAS, claims, transitions) | ✅ |
| `OperationPresenter` | Typed EventBus subscriber; single owner of final delivery | ✅ |
| `EventBus` | In-memory pub/sub, typed events, correlation_id | ✅ |
| `GatewayClient` | HTTP/SSE клиент (submit, steer, cancel, wait_for_terminal) | ✅ |
| `Gateway API` | FastAPI (auth, correlation, events, terminal result) | ✅ |
| `Verifier` | Независимый сервис; sole DONE authority | ✅ |
| `Safety Gate` | Pre-commit sensitive input check, SHA-256 arguments | ✅ |
| `Sanitizer` | Credential redaction, path policy, HTML-escape, empty evidence | ✅ |
| `Queue` | PostgreSQL job queue with lease/retry/heartbeat | ✅ |
| `InputPipeline` | UserInputEnvelope → ContextResolver → IntentRouter → Gateway | ✅ |
| `Telegram Bindings` | Persistent chat_id↔telegram_message_id↔task_id | ✅ |

## Security

- Pre-commit: `SensitiveTaskInput` before any DB write/flush
- SHA-256: tool_arguments, approval arguments — no raw secrets in persistence
- Whitelist projections: flow/replay API hides raw input/output/evidence
- Worker exceptions: fixed categories, no traceback/canary
- Sanitizer: credentials, `.env*`, `.token`, UNC, traversal, empty evidence
- Channel output: no `{exc}` in Telegram, no token-prefix in Dashboard logs
- ActionExecutor: only READ_FILE local; write/shell/code → Gateway

## Database (SQLite / PostgreSQL)

- `operations` — progress messages lifecycle (CAS claims, receipts)
- `task_flows` — Gateway task flows (20-state machine, revisions, artifacts)
- `flow_steps` — execution steps (tool_name, arguments_sha256, output)
- `state_transitions` — append-only transition journal (REJECTED rows too)
- `queue_jobs` — durable queue (lease, retry, heartbeat)
- `artifacts` — verified tool output (sha256, size, verified flag)
- `telegram_message_bindings` — chat_id↔message_id↔task_id
- `approvals` — risk-based approval records
- `durable_operations` — sub-step execution tracking
- `delivery_outbox` — idempotent delivery adapter
- `skills` — registry + versions + transitions
- `cron_schedules` — recurring task schedules
