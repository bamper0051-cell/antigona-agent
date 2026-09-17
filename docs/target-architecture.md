# Целевая архитектура Antigona (интеграционная)

Минимальная целевая схема без переписывания ядра. Каждый контракт маппится на существующие модули
(✅ есть / 🟡 обернуть / ❌ добавить). Пять доноров используются только как референсы/адаптеры.

## Схема

```text
Telegram / CLI / TUI / API
        │  NormalizedInboundMessage (новый: message_id/channel/chat_id/user_id/thread_id/text/
        │                          attachments/reply_to/command/metadata/received_at)
        ▼
ChannelAdapter (🟡 channels/base.py — усилить) ──► CommandRouter (команды → domain action)
        │
        ▼
AntigonaBrain (✅ core/brain.py — не заменять)
        │
        ├── Conversation Engine (✅ conversation/dialogue_engine.py)
        ├── Session History (✅ sessions/repository.py)
        ├── Long-Term Memory Provider (🟡 MemoryRepository + provenance/confidence/scope/expiry)
        ├── Intent / Policy Routing (✅ IntentRouter; PolicyEngine → единый PermissionGate)
        │
        ▼
Durable TaskFlow (✅ models.TaskFlow + state_machine)  ←  AgentRun-события (🟡 run_events)
        │
   ┌────┼────────────┐
   ▼    ▼            ▼
Native Worker   ACPX Adapter   Другие SubagentAdapter
(✅ worker)   (❌ новый, как    (✅ subagents/base.py)
              SubagentAdapter)
        │            │
        └── PermissionGate ──┘  (🟡 объединить RiskClassifier/pin_gate/hitl; allow|deny|ask)
                    │
                    ▼
        Canonical ToolRegistry (❌ один ToolSpec; ✅ tools/contracts.py как основа)
   ┌──────┼─────────────┐
   ▼      ▼             ▼
Local tools   MCP tools      CLI harness tools
(✅)      (✅ mcp/)      (❌ CliHarnessAdapter, quarantine+contract tests)
                    │
                    ▼
            Observer + Verifier (✅ verifier_service — единственный владелец DONE)
                    │
                    ▼
        Durable Delivery Outbox (✅ delivery/)
```

## Контракты и владельцы

| Контракт | Владелец (канон) | Действие |
|---|---|---|
| ToolSpec | **один** канонический (на базе `tools/contracts.py`) | свести 4 контракта (contracts.py Protocol — удалить; registry.Tool/action_executor — adapters) |
| EventBus | `events/bus.py` (+ `core/event_log.py` для задач) | удалить `tasks/event_bus.py` после адаптера |
| Memory | `core/memory_repository.py` (LTM) + `sessions/repository.py` (history) | FileMemory → LEGACY; LongTerm/Postgres/SelfLearning → LEGACY/миграция |
| PermissionGate | единый (на базе pin_gate.RiskClass + hitl.ConfirmationPolicy) | единый policy-path для local/MCP/backend tools; audit; output limits |
| AgentBackend | `SubagentAdapter` (расширить) | NativeAntigonaBackend + AcpxBackend как реализации |
| ChannelAdapter | `channels/base.py` (усилить) | NormalizedInboundMessage; Telegram SDK не в ядре |
| DocumentStore | новый (`documents` + ingestion queue + hybrid retrieval) | после PR-9/10 (memory boundaries) |

## Приоритет интеграции (из аудита)
1. **Унификация внутренних контрактов** (P0.1–P0.4) — обязательный нулевой этап.
2. ACPX adapter (один coding agent; execute/status/cancel; session mapping; permission bridge; healthcheck).
3. CLI-Anything adapter layer (CliHarnessAdapter; manifest; quarantine; contract tests).
4. Telegram streaming UX (buffered edits, throttle, split, heartbeat, task cards).
5. Memory pipeline (candidates, dedup, contradiction, provenance, compaction).
6. Async RAG/hybrid search.
7. Plugin hardening (manifest/capabilities/contract-suite) — без второго plugin loader.
8. Workflow DAG — только после трёх реальных повторяемых процессов.

## Неинтегрируемое
MuseBot core/RAG/прочие каналы; ACPX очередь/permission/state machine/CLI как основной интерфейс;
Memoh код (AGPL — только clean-room идеи); TGO микросервисы/CRM/frontend; LangBot marketplace/multi-tenant;
автоматический write-capable harness без quarantine; Redis/NATS/K8s без доказанной необходимости.

## Критерий готовности
Любая новая интеграция (ACPX/CLI-harness/MCP-tool) подключается как один новый адаптер к каноническому
контракту — без нового ядра, второго EventBus, второго ToolRegistry или обхода PermissionGate/Verifier.
