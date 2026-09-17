# Antigona Decisions (ADR)

## ADR-0002: Worker Python runtime split for OpenHands SDK

- **Date:** 2026-07-25
- **Status:** Accepted
- **Collision note:** `docs/adr/0001-telegram-p0-boundary.md` already occupies `ADR-0001`, so this runtime decision is recorded as `ADR-0002`.

### Context

- The repository baseline targets Python `3.11+`.
- P0.2 requires OpenHands in-process execution with `LocalConversation` and registered clean-room tools.
- OpenHands SDK support is Python 3.12-centric in current releases.

### Decision

- Keep repository/runtime baseline at Python `3.11+`.
- Create a dedicated Worker virtual environment at `.venv-worker312` using `/usr/bin/python3.12`.
- Pin Worker SDK packages to:
  - `openhands-sdk==1.36.1`
  - `openhands-tools==1.36.1`

### Consequences

- Core services and tests remain on repo Python baseline.
- Worker OpenHands integration can advance without forcing a repo-wide runtime migration.
- `src/antigona/worker/agent_core.py` includes an SDK integration seam for `LocalConversation(persistence_dir, conversation_id)` and a scripted fallback for offline/incompatible operation.
- P0 web-fetch remains a no-network stub by policy.

## ADR-0005: Clean-room skill format `ASKILL/1` and registry trust boundary

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0005-skills-clean-room-format.md`](adr/0005-skills-clean-room-format.md)
- **Specification:** [`docs/SKILL_FORMAT.md`](SKILL_FORMAT.md)

### Decision (summary)

- Skills use the own `ASKILL/1` format (`.askill`), not YAML-frontmatter + Markdown; no YAML, no JSON, no frontmatter separators.
- A skill is **data, not code**: registered tool references plus typed arguments only; no shell, `eval`, include, env vars, or external file references.
- Promotion to `ACTIVE` is a Verifier-only privilege (bearer + CAS on `revision`), mirroring the "only Verifier sets `DONE`" rule; `QUARANTINED` is sticky.
- The skills registry stays on SQLite next to durable state; PostgreSQL migration remains P4 (see ADR-0003).
- Cards captured from untrusted trajectories inherit `trust untrusted` and a hard `LOW` risk ceiling (P1.3 trust degradation).
- Clean-room: skill implementations in `.venv-worker312` (`openhands`, `anthropic`, `fastmcp`) and AGPL klio-tech sources must not be read or borrowed.
- Consistent with ADR-0004: verification criteria live only in `VerifierCriteriaStore`; the parser rejects criteria-like sections and keys (`E-CRITERIA`).

## ADR-0006: Cron scheduler architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0006-cron-scheduler-architecture.md`](adr/0006-cron-scheduler-architecture.md)

### Decision (summary)

- Cron schedules have no state machine (`enabled`/`cancelled` booleans only); there is no `DONE`/`VERIFYING` path.
- Tick mechanism: FastAPI lifespan BackgroundTask (in-process async loop) — not a separate service.
- Owner isolation on all endpoints (same pattern as flows/skills).
- Sticky cancel is the only termination mechanism (no DELETE endpoint).
- Append-only journal `schedule_events` for lifecycle audit trail (`created`/`ticked`/`errored`/`cancelled`).
- Durable layer stays on SQLite until P4.
- Clean-room: only `croniter` (MIT) for expression parsing; no upstream cron libraries.

## ADR-0007: Replay-UI architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0007-replay-ui-architecture.md`](adr/0007-replay-ui-architecture.md)

### Decision (summary)

- `ReplayEngine` is a read-only projection over `task_flows`, `flow_steps`, `state_transitions` and `artifacts`: no `session.add`/`flush`/`commit`, no state-machine call, no verifier credential — replay therefore has no path to `DONE` (consistent with ADR-0004).
- Owner isolation returns **404, never 403**: "not yours" is indistinguishable from "does not exist", so the endpoint is not an existence oracle.
- One dataclass projection, three renderings: JSON (`ReplayResponse`), Rich text (`Panel` + `Table` + `Tree` + `Table`), Textual widgets (`DataTable` + `Tree`). `ReplayTrajectory.from_dict()` lets the CLI reuse the engine's own renderer on Gateway JSON.
- `actor` / `entity_type` / `from_dt` / `to_dt` narrow the transition journal only; steps, artifacts and the flow header are always returned in full. Time bounds are normalized to naive UTC to match `models.utcnow()`.
- `get_timeline()` derives a flat chronology (`transition` / `step` / `rejected` / `artifact`) from existing tables — no new table, no new write path.
- Out of scope by decision: graph visualizations, WebSocket live replay, HTML/PDF export, trajectory diffing, replay-mode editing.
- Clean-room: no upstream tracer (`celery.result`, `temporal`, `prefect`, `mlflow`, `langsmith`, `langfuse`, `phoenix`, `opentelemetry`); verified by `tests/unit/test_clean_room_replay.py`.

## ADR-0008: TUI architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0008-tui-architecture.md`](adr/0008-tui-architecture.md)

### Decision (summary)

- The Textual TUI is a **thin client**: it holds no `Session`, no `TaskRepository` and no verifier credential, and its only relative import is `GatewayClient`. It therefore has no path to the Verifier-owned terminal state (consistent with ADR-0004) and does not even name it — the status palette is keyed on lowercase strings.
- Four tabs (Flows / Live / Approvals / Replay) via `Tabs` + `ContentSwitcher` over one shared event `Log`; all panes stay in the DOM and keep their state. `TabbedContent`/`TabPane` was rejected because its compose form needs a running app.
- Three new **read-only** Gateway endpoints: `GET /flows`, `GET /approvals?status=PENDING`, `GET /approvals/{id}` — owner-scoped, paginated (`limit` clamped to 200), `log_event`-audited, `SELECT`-only. Approvals are scoped through a join on `task_flows.owner_id`.
- Owner isolation answers **404, never 403** (inherited from ADR-0007): a foreign approval id is indistinguishable from a missing one.
- Writes reuse the existing endpoints only — `POST /approvals/{id}/decision` and `POST /flows/{id}/cancel`; P2.4 adds no new write path.
- Live updates run over the existing `WS /flows/{id}/progress` through `GatewayClient.stream_progress_ws()` (async generator) inside one exclusive, cancellable Textual worker; polling `GET /flows` stays as the fallback.
- Table content is produced by pure projection functions over dicts (`flow_rows`, `approval_rows`, `transition_rows`, `status_color`, …), so rendering is unit-testable without a running app.
- Entry point `antigona tui`; importing `antigona.tui` starts nothing, which is what enables headless `run_test()`.
- Deliberate deviation from P2_4_PLAN.md §2.6: `#replay_status_filter` filters transitions client-side by target state, because the replay endpoint's `entity_type` is a different axis (task vs step) and must not be mislabelled as a status filter.
- Out of scope by decision: flow creation from the TUI, web UI, SSE, graph visualizations, multi-owner admin panel.
- Clean-room: only `textual` (MIT) + `rich` + `websockets` + `httpx`, all pre-existing. No upstream agent shell and no alternative TUI toolkit, verified by `tests/unit/test_clean_room_tui.py`.

## ADR-0009: Subagents architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0009-subagents-architecture.md`](adr/0009-subagents-architecture.md)

### Decision (summary)

- **Parallelism = independent durable flows**, not in-process threads: a child is an ordinary `TaskFlow` row (own state/lease/steps/artifacts, `parent_id` set) that the same `DurableQueue` and workers pick up. Isolation follows because each `Orchestrator.run` is its own transaction.
- The child **always inherits** the parent's `owner_id` (cross-owner spawning impossible) and its `max_depth`/`max_child_budget` envelope, so one budget governs the whole workstream tree.
- **Depth and budget gates run before any insert**: `parent.depth + 1 > max_depth` → `DepthLimitExceeded`; `count(children) >= max_child_budget` → `BudgetLimitExceeded`. No half-created child is ever rolled back — the primary defence against recursive child explosion.
- **Idempotency:** a repeated `spawn_child_flow` with the same explicit `idempotency_key` returns the existing child unchanged (no duplicate, no spurious budget trip), backed by the `uq_owner_idem` constraint and a `payload_fingerprint`.
- **Aggregation is read-only:** `aggregate_child_results` is a pure `SELECT` returning `{total, done, failed, pending, all_done, children+artifacts}` with no state-machine transition and no write — the "only the Verifier sets DONE" invariant (ADR-0004) is preserved.
- **Failure isolation** is behavioural: a child that fails verification transitions to `FAILED` in its own transaction; the parent and any DONE sibling are untouched, and `aggregate` reports `failed_count>=1, all_done=False`.
- **Known limitation:** no cancel-propagation in P3.1 (parent cancel does not cascade to children); deferred to a later step and documented, not a defect.
- Out of scope by decision: swappable execution backends (P3.2/P3.3), dual-LLM defence (P3.3), the optional `POST /flows/{id}/children` endpoint, planner auto-spawning, cross-owner subagents.
- Clean-room: subagent logic borrows nothing from klio-tech/Hermes/OpenClaw/OpenHands, verified by `tests/unit/test_clean_room_subagents.py`.

## ADR-0010: Workspace execution backends

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0010-workspace-backends.md`](adr/0010-workspace-backends.md)

### Decision (summary)

- **`BaseWorkspace` is the sole execution surface:** `WorkerAgentCore` delegates all file/shell operations through `self.workspace.write_file`, `self.workspace.read_file`, `self.workspace.execute_command`. Direct access to local tool objects (`self.file_tools`, `self.shell_tool`) is removed.
- **Dual Mock / Real adapter architecture:** Remote backends (`docker`, `ssh`, `modal`, `daytona`) provide Mock classes (zero network/SDK dependency, safe for CI) and Real classes (lazy SDK import, `WorkspaceConnectionError` on failed connection).
- **Backend configuration without code modification:** `WorkspaceFactory.create_workspace` resolves backends from `Settings` / env (`ANTIGONA_WORKSPACE_BACKEND`, `ANTIGONA_WORKSPACE_MOCK`, backend-specific credentials). Custom backends register dynamically via `WorkspaceFactory.register()`.
- **Per-task workspace isolation:** When `task_id` is supplied, root path is scoped to `<workspace>/<task_id>`. `ws.cleanup()` removes task directories or stops remote sandboxes. Subagents inherit the parent flow's workspace backend.
- **Clean-room:** Execution backends borrow nothing from AGPL klio-tech/Hermes/OpenClaw, verified by `tests/unit/test_clean_room_backends.py`.

## ADR-0011: Dual-LLM quarantine model

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0011-dual-llm-quarantine-model.md`](adr/0011-dual-llm-quarantine-model.md)

### Decision (summary)

- **Tool-less Quarantine Model:** Untrusted content (from web fetch or untrusted file read) is sanitized by an isolated, tool-less LLM instance (`QuarantineModel`) before reaching the primary agent's reasoning context.
- **Model Collision Guard:** `quarantine_model` must differ from `model_primary`; collision raises `ModelCollisionError`.
- **Dual Mock / Real Adapter Architecture:** Deterministic `MockQuarantineProvider` for offline/CI environments and `HTTPQuarantineProvider` with lazy `urllib` import for real external models.
- **Fail-Closed Transport Handling:** Real transport errors raise `QuarantineUnavailableError`, triggering `_degrade_trust()` and returning `[QUARANTINE_UNAVAILABLE]`. Raw bytes never enter reasoning.
- **Backstop Intact:** Existing `_degrade_trust()` and `untrusted_context` shell blocking remain active as backstop security controls.
- **Clean-room:** No code, identifiers, or concepts borrowed from AGPL klio-tech/Hermes/OpenClaw, verified by `tests/unit/test_clean_room_quarantine.py`.

## ADR-0012: Smart egress-proxy с allowlist

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0012-egress-proxy.md`](adr/0012-egress-proxy.md)

### Decision (summary)

- **Smart Egress Proxy:** `EgressProxy` acts as the sole transport layer for network tools like `WebFetchTool`.
- **Deny-by-Default Allowlist:** Domains must match `Allowlist` exact host or suffix template (`*.example.com`). Blocked domains raise `EgressUnavailableError`.
- **Fail-Closed Transport:** Network errors or allowlist blocks result in `WebFetchResult(enabled=False, detail="egress blocked: ...")`, preventing raw content leakage or open network access.
- **Quarantine Integration (P3.3):** Raw content returned via `EgressProxy` continues to route through `quarantine.sanitize()` in `WorkerAgentCore`.
- **Clean-room:** In-process proxy implementation without container spawning via `docker.sock`; verified by `tests/unit/test_clean_room_egress.py`.

## ADR-0013: Micro-VM (Firecracker / E2B) isolation for high-risk tools

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0013-micro-vm-isolation.md`](adr/0013-micro-vm-isolation.md)

### Decision (summary)

- **Micro-VM Isolation for High-Risk Tools:** High-risk shell commands (outside the P0 allowlist or with path separators) route to `MicroVMRunner` (Firecracker or E2B).
- **Fail-Closed Availability Check:** If micro-VM runtime is configured (`firecracker` or `e2b`) but unavailable on host, execution is blocked with `MicroVMUnavailableError`. No silent host fallback.
- **Egress Proxy Integration (P4.1):** Guest micro-VM networking routes exclusively through `EgressProxy` (P4.1) for deny-by-default domain filtering.
- **Security Invariants:** Never pass `docker.sock` to the VM; mark output as `untrusted=True`.
- **Clean-room:** Clean-room implementation verified by `tests/unit/test_clean_room_microvm.py`.


## ADR-0014: Async Postgres + Redis для durable-слоя

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0014-postgres-redis.md`](adr/0014-postgres-redis.md)

### Decision (summary)

- **Async SQLAlchemy (`antigona.storage`):** два диалекта — `postgresql+asyncpg` (primary) и `sqlite+aiosqlite` (fallback и **default**). Legacy-URL нормализуются на async-драйвер; неизвестный драйвер отвергается на этапе конфигурации.
- **Единая metadata:** async-слой переиспользует ORM-модели `antigona.models` — дрейф схем между sync и async путями невозможен по конструкции.
- **Alembic — единственный путь эволюции схемы Postgres:** `storage/migrations/0001_initial` строит схему из общей metadata и ставит append-only триггеры (`RAISE EXCEPTION` в PG, `RAISE(ABORT)` в SQLite). CLI: `antigona db upgrade`.
- **Redis — транспорт и кэш, НЕ источник истины:** `RedisTaskBroker` только будит воркеров (RPUSH/BLPOP), проснувшийся воркер всё равно выигрывает `DurableQueue.claim()` через CAS в SQL; `StateCache` пишет состояние только **после commit** и читается cache-aside (miss → БД).
- **Fail-closed / fail-soft:** недоступный Postgres → retry с backoff, затем `StorageUnavailableError` и остановка старта; недоступный Redis → warning и деградация на чистый SQL-путь без потери функциональности.
- **Обратная совместимость:** default остаётся sqlite, `redis_url=None` = поведение до P4.3, sync `database.py` сохраняется (deprecation-путь, не удаление).
- **Clean-room:** проверено `tests/unit/test_clean_room_storage.py` (нет klio/Hermes/OpenClaw; брокер и кэш не пишут переходы).


## ADR-0015: Multi-channel Delivery Adapters (Discord, Slack, WhatsApp, Signal, Email)

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** [`docs/adr/0015-delivery-adapters.md`](adr/0015-delivery-adapters.md)

### Decision (summary)

- **Unified Delivery Interface:** Every channel implements `DeliveryAdapter.deliver(event, idempotency_key)`; state transitions remain Verifier-owned.
- **Factory & Router (`factory.py`, `router.py`):** `get_adapter` builds adapters from settings; unknown channel raises `UnknownChannelError` (no silent Telegram fallback); `adapter="progress"` maps to default channel.
- **Fail-Closed & Fail-Safe:** API failure leaves outbox row in `PENDING` with retry backoff and `last_error`; no dropped messages or false DONE.
- **Dual Mock / Real & Lazy SDK:** Mock mode (`delivery_mock=True`) logs without network; real mode lazily imports SDKs inside `deliver()`.
- **Clean-room:** Verified by `tests/unit/test_clean_room_delivery.py`.

