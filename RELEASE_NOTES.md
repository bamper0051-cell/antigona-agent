# Antigona v1.1.0 — Release Notes (2026-07-28)

## 🎯 Что нового

### 🧠 Single Runtime (единый execution контур)
- **Устранены 3 параллельных контура** — все task intents идут через Gateway.
- ActionExecutor bypass удалён: `_SIMPLE_TASK_INTENTS`, `_handle_plan_or_actions` → только Gateway.
- Единая 20-state state machine: `models.TaskState` поглотил `TaskFlowStateMachine`.
- `RETRY_SCHEDULED`, `REPLAN_REQUESTED`, `WAITING_USER`, `PAUSED` — теперь в authoritative Gateway.

### 📋 Operation Lifecycle (progress → final → cleanup)
- `OperationStore` — durable async repository с CAS, claims, manifest.
- `OperationPresenter` — единственный владелец progress и final delivery.
- Typed EventBus события: `OperationReceived`, `StageChanged`, `ToolProgress`, `FinalResponseReady`.
- Ровно один progress → edits → один final → receipt → cleanup progress.
- `EventBus.publish()` ≠ delivery ack; только реальный Telegram receipt.
- At-most-once: claim-before-send, manifest-after-send, terminal-commit-after-manifest.

### 🔒 Security P0
- **Pre-commit boundary**: `SensitiveTaskInput` до любых DB write/flush.
- **SHA-256** в `tool_arguments`, `FlowStep.input`, `Approval.arguments`.
- **Whitelist-проекции**: flow/replay API закрывает raw input/output/evidence.
- **Sanitizer**: credentials (quoted/escaped/percent-encoded), path (traversal/UNC/symlink/`.env*`/`.token`), empty evidence → fail-closed.
- **Worker exceptions**: фиксированные категории, ни traceback, ни canary.
- **Verifier**: hardened descriptor read, `is_usable_result_text`.
- **Telegram**: ни одного `f"❌ ... {exc}"` — все safe тексты.
- **Gateway waiter**: monotonic deadline, final GET при cancellation, revision agreement, fail-closed DONE.

### 🚀 Gateway Waiter (Claude Code Opus)
- `wait_for_terminal()` — bounded HTTP reads, единый deadine.
- Cancellation → authoritative final GET, не remote cancel.
- Revision mismatch → `GatewayProtocolError`.
- DONE fail-closed: без непустого verified артефакта → error.

### 📦 Complete Pack Gap Analysis
- Пакет `ANTIGONA_AGENT_CORE_COMPLETE_PACK_20260728.zip` проверен (105 checksum OK).
- Все gaps зафиксированы и закрыты (ActionExecutor bypass, Single Runtime).

## 🧪 Тесты

| Suite | Результат |
|-------|----------|
| Lifecycle hardening + Presenter | 42 passed |
| Security (result safety, input safety, worker) | Clean |
| Gateway result API + terminal client | Clean |
| InputPipeline + Telegram pipeline | Clean |
| TaskFlow state machine (92 parametrized) | Clean |
| **Combined focused run** | **256 passed** |
| Ruff | All checks passed |
| Compileall | OK |
| `git diff --check` | Clean |

## 📊 Метрики

- Модулей: 135+
- Тестов: 1000+
- Commits на ветке: 4 (24cebd93, 3e5b457e, 006b2c1c, doc-update)
- Изменено файлов: 47 + 1 + 6 + 3
- Добавлено строк: 10 460 + 25 + 282 + doc
- Удалено строк: 653 + 108 + 425 + stale

## 🛠 Запуск

```bash
bash /usr/local/bin/start_antigona_stack.sh
```

Gateway :8090, Verifier :8091, 5 процессов.

## ⚠️ Остаточные риски

1. **At-most-once crash window**: Telegram send успешен → процесс падает до manifest → final потерян. Claim остаётся in-flight, но receipt требует reconciliation. **Не exactly-once.**
2. **Три процесса**: Gateway, Verifier, Worker — единый код, но отдельные процессы. Ручной рестарт при сбое.
