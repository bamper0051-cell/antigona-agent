# Инвентаризация архитектурных контрактов

PR-01 фиксирует текущее состояние без изменения runtime-поведения. Статус
`legacy` означает существующий путь, который нельзя расширять; `deprecated`
означает путь, предназначенный к удалению после adapter-миграции.

## Tool

| Контракт | Тип | Статус | Единственный владелец | Миграция |
|---|---|---|---|---|
| `src/antigona/contracts.py::Tool` | `Protocol[InputT]` | legacy | `src/antigona/tools/contracts.py::ToolSpec` | не начата; запрет новых импортов |
| `src/antigona/tools/contracts.py::Tool` | `ABC` | deprecated | `src/antigona/tools/contracts.py::ToolSpec` + будущий handler boundary | compatibility adapter, затем удалить |
| `src/antigona/tools/contracts.py::ToolSpec` | `dataclass` | canonical target | `src/antigona/tools/contracts.py` | владелец назначен PR-01 |
| `src/antigona/tools/registry.py::Tool` | descriptor `dataclass` | legacy | `src/antigona/tools/contracts.py::ToolSpec` | adapter для descriptor, затем удалить |

## EventBus

| Контракт | Тип | Статус | Единственный владелец | Миграция |
|---|---|---|---|---|
| `src/antigona/events/bus.py::EventBus` | typed async pub/sub class | canonical | `src/antigona/events/bus.py` | текущий runtime-канон |
| `src/antigona/tasks/event_bus.py::EventBus` | task-local async bus class | deprecated | `src/antigona/events/bus.py::EventBus` | завернуть adapter-ом, затем удалить |
| `src/antigona/tasks/event_bus.py::EventHandler` | callback type alias | legacy | `src/antigona/events/bus.py::EventHandler` | заменить вместе с legacy bus |

## Memory и Context

| Контракт | Тип | Статус | Единственный владелец | Миграция |
|---|---|---|---|---|
| `src/antigona/context/builder.py::ContextBuilder` | conversation context builder | canonical для conversation history | `src/antigona/context/builder.py` | использовать только для текущего диалога |
| `src/antigona/memory/long_term.py::LongTermMemory` | in-process long-term store | legacy | `src/antigona/core/memory_repository.py::MemoryRepository` | adapter/перенос provenance и scope |
| `src/antigona/core/memory_repository.py::MemoryRepository` | repository facade | canonical target для user/project memory | `src/antigona/core/memory_repository.py` | владелец назначен PR-01 |
| `src/antigona/memory/file_memory.py::FileMemory` | file-backed memory | legacy backend | `src/antigona/core/memory_repository.py` | подключать только через repository adapter |
| `src/antigona/memory/postgres_memory.py::PostgresMemoryStore` | Postgres backend | legacy backend | `src/antigona/core/memory_repository.py` | подключать только через repository adapter |
| `src/antigona/memory/summarizer.py::MemorySummarizer` | compression helper | supporting, не самостоятельный memory contract | `src/antigona/context/builder.py` | оставить helper-ом conversation context |

## Session, Run, Subagent, Approval

| Контракт | Тип | Статус | Единственный владелец | Миграция |
|---|---|---|---|---|
| `src/antigona/sessions/repository.py::SessionRepository` | application session repository | canonical target | `src/antigona/sessions/repository.py` | владелец назначен PR-01 |
| `src/antigona/sessions/database.py::SessionDatabase` | SQLite/session persistence facade | deprecated | `src/antigona/sessions/repository.py::SessionRepository` | adapter, затем удалить |
| `src/antigona/database.py::Database.session()` | SQLAlchemy unit-of-work factory | infrastructure, не domain Session | `src/antigona/database.py` | не смешивать с application session |
| `src/antigona/agent/loop.py::AgentLoop::run` и `::LoopResult` | legacy autonomous run API/result | legacy | `src/antigona/durable/agent_loop.py::AutonomousLoop` | adapter/перевод после PR-02+ |
| `src/antigona/durable/agent_loop.py::AutonomousLoop` и `::LoopResult` | durable run API/result | canonical target | `src/antigona/durable/agent_loop.py` | владелец назначен PR-01 |
| `src/antigona/subagents/base.py::SubagentAdapter` и `::SubagentResult` | protocol/result | canonical target | `src/antigona/subagents/base.py` | текущий subagent boundary |
| `src/antigona/subagents/registry.py::SubagentRegistry` | registry/service | canonical owner of discovery | `src/antigona/subagents/registry.py` | не создавать новые registries |
| `src/antigona/models.py::Approval` | persisted approval entity | canonical | `src/antigona/models.py` | текущий владелец состояния и переходов |
| `src/antigona/schemas.py::ApprovalView`, `::ApprovalListEntry`, `::ApprovalListView` | API schemas | canonical transport projection | `src/antigona/schemas.py` | использовать вместо дубликатов view |
| `src/antigona/core/control_plane.py::ApprovalView`, `::ApprovalListEntry`, `::ApprovalListView` | duplicate control-plane DTOs | legacy | `src/antigona/schemas.py` | заменить ссылками на transport schemas |

## Правила для новых PR

1. Не добавлять новые импорты `antigona.contracts.Tool`,
   `antigona.tools.contracts.Tool`, `antigona.tools.registry.Tool` и
   `antigona.tasks.event_bus`; исключение — явные compatibility adapters.
2. Не импортировать vendor SDK (`openai`, `anthropic`, `google.generativeai`,
   `deepseek`) из доменных модулей. Такие импорты разрешены только в каталогах
   `*/adapters/*`.
3. Новые доменные зависимости направлять на владельцев из таблиц, не создавать
   параллельные `Session`, `Run`, `Memory`, `Subagent` или `Approval` DTO.
4. Любое снятие baseline сопровождается отдельной миграцией и тестом; baseline
   не используется для одобрения новых архитектурных отклонений.
