# P0 architecture and trust boundaries

This document does not weaken the original architecture specification. It records the concrete P0 implementation.

## Processes

1. **Gateway (`antigona-gateway`)** owns channel auth, owner isolation, idempotent task creation, approvals, cancellation and durable enqueue. It has no Verifier credential and never executes a task.
2. **Worker (`antigona-worker`)** atomically claims `queue_jobs`, renews leases, recovers expired claims, invokes a replaceable Planner, applies policy/approval, runs Docker tools and requests verification through authenticated HTTP.
3. **Verifier (`antigona-verifier`)** is a separate FastAPI service. It alone contains the narrow SQL update `VERIFYING -> DONE`, guarded by bearer credential and revision CAS. Runtime/repository modules contain no finalizer class, token or DONE mutation path.

The authenticated HTTP boundary prevents an unauthenticated caller from finalizing (the negative probe requires HTTP 401), but SQLite cannot grant per-table or per-statement roles. Any process with write access to the database file can bypass the service and issue raw SQL. A strict deployment boundary therefore requires distinct OS users and file/directory ACLs; PostgreSQL should instead grant a dedicated Verifier role only the finalization function.

## Durable records

The baseline migration creates: `task_flows`, `flow_steps`, `state_transitions`, `artifacts`, `approvals`, `queue_jobs`, `durable_operations`, `delivery_outbox`, plus `schema_version`. Task/step mutations use revision CAS. Queue claims have owner, expiry and heartbeat. Durable operations are written PREPARED before a tool and APPLIED immediately after evidence exists, allowing crash recovery without duplicate side effects. `WAITING_APPROVAL` is persisted and approval requeues the existing unique job.

## Planner → Executor → Verifier

`pipeline.py` defines typed protocols. `DeterministicPlanner` is only the P0 implementation; orchestration receives a `Planner` and `CompletionVerifier` by dependency injection. Completion is a request, never a runtime status assignment.

## Sandbox policy

Docker is the default and fail-closed backend. It uses no network, read-only root, capability drop, no-new-privileges, a non-root UID/GID, CPU/RAM/PID limits, tmpfs and timeout. Only a `0750` workspace is mounted read-write. Shell accepts argv (no host-shell interpolation), caps output and workspace bytes, and tracks a named container so cancellation kills the actual Docker workload and yields `cancelled`. P0's byte quota is a non-atomic sum of regular files checked before and after execution; it does not prevent temporary overage or concurrent-writer races. A kernel/project quota on a dedicated filesystem is required for those guarantees.

## Delivery and observability

`DeliveryAdapter` decouples progress from runtime. `TelegramAdapter` is send-only; Gateway remains the sole channel/polling owner. `FakeAdapter` gives hermetic contract tests. JSON logs carry correlation/task/session/step/tool/duration/status and redact keys/values that look secret-bearing.

## Verification evidence

- Unit/integration/recovery suite, branch coverage gate ≥85%.
- Empty-DB migration test compares SQL-created tables with ORM metadata.
- Multi-process E2E launches three distinct PIDs and proves Gateway → SQLite queue → worker → Docker file write → authenticated Verifier → DONE.
- The same E2E probes Verifier without its credential and requires HTTP 401.
- Docker shell E2E proves file mutation, workspace mode and live container cancellation.

The authoritative product scope remains the original architecture document; this P0 does not claim P1 memory, skills, replay, browser, Web UI or production PostgreSQL/Redis capabilities.

## P1 memory boundary

P1 memory uses a dedicated PostgreSQL database with pgvector while durable task state, queue and outbox remain on SQLite. This is intentionally not the P4 durable migration: no measured SQLite contention or `SKIP LOCKED` requirement exists yet. `PostgresMemoryStore` owns idempotent memory-schema migration, owner-isolated session memories, profile upserts and cosine retrieval. Embeddings are explicit typed inputs; this layer performs no network calls and does not silently select an embedding model. See ADR-0003.

## P2 skills boundary

P2.1 introduces a clean-room skill system with its own `ASKILL/1` format (`.askill`), independent of YAML/Markdown formats.

1. **Format & Verification:** Line-based single-pass parser and canonicalizer (`skills/format.py`, `skills/canonical.py`). Every card is content-addressed by its `%end sha256:... bytes=...` footer digest.
2. **Lifecycle State Machine:** `DRAFT -> CANDIDATE -> ACTIVE -> DEPRECATED | QUARANTINED | REJECTED`. `CANDIDATE -> ACTIVE` is strictly Verifier-only via bearer-authenticated CAS (`POST /skills/{id}/promote`). Neither Gateway nor Worker can write `ACTIVE` status.
3. **Store & Security:** Cards are stored in a content-addressed filesystem store (`skills/` under state root) with strict permissions (`0750` dir, `0640` file). Hardlinks (`st_nlink > 1`), symlinks, and path escapes are rejected. Digest mismatch immediately quarantines the skill.
5. **Deterministic Matcher:** Trigger matching (`skills/matcher.py`) is owner-isolated, fully deterministic (no LLM/network), and only selects `ACTIVE` skills.
6. **Trust Inheritance:** Skills captured (`skills/capture.py`) from trajectories touching untrusted content inherit `%trust untrusted` and are capped at `LOW` risk ceiling.

## P2.2 cron scheduler

P2.2 introduces a durable cron scheduler on top of `queue_jobs`.

1. **Model:** `CronSchedule` table for schedule definitions, `ScheduleEvent` append-only journal for lifecycle tracking.
2. **Execution:** `CronScheduler.tick()` polls for due schedules, creates `TaskFlow` via `TaskRepository` + `DurableQueue` (standard pipeline). Idempotency key: `cron:{schedule_id}:{next_run_at}`.
3. **Tick trigger:** Background asyncio task in Gateway lifespan (configurable interval, default 30s). Manual trigger via `POST /schedules/tick`.
4. **Lifecycle:** No state machine. Boolean flags `enabled`/`cancelled`. Sticky cancel (`cancelled=true, enabled=false`) — no DELETE path.
5. **Owner isolation:** All endpoints filter by `owner_id`, matching flows/skills pattern.
6. **REST API:** 5 endpoints: `POST/GET /schedules`, `GET /schedules/{id}`, `GET /schedules/{id}/jobs`, `POST /schedules/{id}/cancel`, `POST /schedules/tick`.
7. **CLI:** `antigona cron create|list|show|jobs|cancel|tick`.
8. **Clean-room:** Only `croniter` (MIT, already a dependency) for expression parsing. No upstream cron libraries. See `docs/adr/0006-cron-scheduler-architecture.md`.


## P2.3 Replay-UI

P2.3 adds a read-only projection of a flow's recorded trajectory. It introduces no table, no write path and no new dependency.

1. **Engine:** `ReplayEngine` (`src/antigona/replay.py`) issues plain `SELECT`s against `task_flows`, `flow_steps`, `state_transitions` and `artifacts`. It never calls `session.add`/`flush`/`commit`, never touches the state machine, and imports neither the verifier nor `TaskRepository` — so observing a flow cannot perturb it, and cannot reach `DONE`.
2. **Projection:** dataclasses `StepReplay`, `TransitionReplay`, `ReplayTrajectory`, `TimelineEvent`, with `to_dict()`/`from_dict()` for a lossless JSON round-trip. `__all__` pins the module's public surface.
3. **Owner isolation:** `get_trajectory(task_id, owner_id)` raises `ReplayTaskNotFound` both for an unknown id and for a flow owned by somebody else; the Gateway maps that to **404, never 403**.
4. **Filtering:** `actor`, `entity_type`, `from_dt`, `to_dt` narrow the transition journal only. Time bounds are normalized to naive UTC (SQLite drops `tzinfo`; see `models.utcnow`). `get_rejected_transitions()` selects the `REJECTED ` rows written by `record_rejection`.
5. **Timeline:** `get_timeline()` merges transitions and artifacts into one chronology tagged `transition` / `step` / `rejected` / `artifact`. `flow_steps` has no timestamp column, so steps appear through their step-entity transitions.
6. **REST API:** `GET /flows/{id}/replay` (typed `ReplayResponse`, four query filters) and `GET /flows/{id}/replay/timeline` (`TimelineResponse`). Both are GET-only and require a bearer token.
7. **CLI:** `antigona replay <flow_id> [--timeline] [--json] [--actor X] [--entity-type task|step] [--from ISO] [--to ISO]`. `--json` writes unformatted JSON to stdout so it can be redirected or piped.
8. **TUI:** the Replay button fills a Textual `DataTable` (`#`, `from -> to`, `actor`, `reason`, `time`) and a step `Tree` with input/output, with `actor`/`status` filter inputs. No live subscription — refresh is an explicit button press.
9. **Colour scheme:** green for success states, red for failure states, yellow for `REJECTED` rows, dim grey for cancelled.
10. **Clean-room:** no upstream tracing library. See `docs/adr/0007-replay-ui-architecture.md`.


## P2.4 Textual TUI

P2.4 promotes the draft Textual screen into the operator console. It introduces no table and no new dependency; the only new server code is three read-only endpoints.

1. **Thin client:** `AntigonaApp` (`src/antigona/tui.py`) reaches the system exclusively through `GatewayClient` — HTTP plus one WebSocket. It imports no `Session`, no `TaskRepository`, no `Database`; its single relative import is `from .cli import GatewayClient`. It therefore has no path to the Verifier-owned terminal state and never names it (the status palette is keyed on lowercase strings).
2. **Tabs:** a `Tabs` bar drives a `ContentSwitcher` over four panes — Flows, Live, Approvals, Replay — mapped by the module-level `TAB_SPECS`. All panes stay mounted, so switching preserves scroll position and filters. One shared `Log` below the switcher records every action.
3. **Flows:** `GET /flows` fills `ID │ Goal │ Status │ Rev │ Created`, row key = flow id, status colour-coded (green success, red failure, yellow waiting, blue in-flight, grey cancelled/timeout). A timer re-polls at `refresh_interval` seconds (`0` disables).
4. **Live:** selecting a flow starts one exclusive Textual worker over `GatewayClient.stream_progress_ws()`, an async generator on the existing `WS /flows/{id}/progress`. Rows land as `# │ From -> To │ Actor │ Reason │ Time`; `end`/`error` frames close the stream. Selecting another flow cancels the previous socket, `on_unmount` cancels the last, and a failure degrades to `stream: disconnected` plus a `Reconnect` button.
5. **Approvals:** `GET /approvals?status=PENDING` fills `ID │ Flow │ Tool │ Risk │ Reason` with the risk level colour-coded. Approve/Reject (buttons or `a`/`x`) call the pre-existing `POST /approvals/{id}/decision`; the decided row is dropped from the table. No new write path was added.
6. **Replay:** the P2.3 trajectory behind nested tabs — transitions `DataTable`, steps `Tree` with input/output, artifacts `DataTable`. `#replay_actor_filter` is passed to `GET /flows/{id}/replay` as `actor`; `#replay_status_filter` narrows target states client-side. When the flow is waiting on an approval, the newest transition row is highlighted.
7. **New endpoints:** `GET /flows` (`FlowListView`), `GET /approvals` (`ApprovalListView`), `GET /approvals/{id}` (`ApprovalView`). All owner-scoped — approvals through a join on `task_flows.owner_id` — `SELECT`-only, `limit` clamped to `MAX_PAGE_SIZE = 200`, each emitting a correlation-carrying `log_event`. Foreign ids answer 404, never 403.
8. **Client:** `GatewayClient` gained `list_flows`, `list_approvals`, `get_approval`, `progress_ws_url`, `stream_progress_ws`, and an optional `transport` argument that lets the same client be pointed at an in-process ASGI app. Existing method signatures are unchanged.
9. **Pure projections:** `flow_rows`, `approval_rows`, `transition_row(s)`, `step_labels`, `artifact_rows`, `status_color`, `risk_color` operate on plain dicts and touch no widget, so table content is asserted without a running app; the widget layer only adds colour and row keys.
10. **Entry point:** `antigona tui [--gateway URL] [--token T] [--refresh SECONDS]`; `antigona-tui` and `python -m antigona.tui` still work. Importing the module starts nothing (`__all__ = ["AntigonaApp", "main"]`), which is what makes headless `run_test()` viable.
11. **Clean-room:** `textual` (MIT) + `rich` + `websockets` + `httpx`, all pre-existing. No upstream agent shell and no alternative TUI toolkit — see `docs/adr/0008-tui-architecture.md`, enforced by `tests/unit/test_clean_room_tui.py`.


## P3.1 Subagents + parallel workflows

P3.1 lets a parent `TaskFlow` spawn child flows (subagents), run them as independent durable workstreams, and aggregate their results under depth/budget limits with full failure isolation.

1. **Parallelism model:** a child is an ordinary `TaskFlow` row with its own state, lease, steps and artifacts and `parent_id` pointing at the parent. Children enter the same `DurableQueue` and are claimed by whichever worker is free — "parallel" means N independent rows, not a thread pool. No separate executor is introduced.
2. **Data model:** `TaskFlow` carries `parent_id` (self-FK, `ondelete=CASCADE`, indexed), `depth` (default 0), `max_depth` (default 3) and `max_child_budget` (default 5). The `parent_id` index backs both `count(children)` and aggregation.
3. **Spawn (`Orchestrator.spawn_child_flow`):** idempotency lookup → depth gate → budget gate → create child `TaskFlow` + first `FlowStep` → commit. The child inherits `owner_id`, `max_depth`, `max_child_budget`; `depth = parent.depth + 1`. Both gates raise (`DepthLimitExceeded`/`BudgetLimitExceeded`) *before* `session.add`, so no half-created child is ever rolled back.
4. **Idempotency:** an explicit `idempotency_key` that already resolved to a child of this parent+owner returns that same row — a retry never duplicates and never spuriously trips the budget gate. Backed by the `uq_owner_idem` constraint and a `payload_fingerprint = sha256(owner_id:goal:target_path:content)`.
5. **Execution:** children run through the same `Orchestrator.run(child, worker_id)` as any task (acquire lease → resume state machine → approval → tool exec → verifying → Verifier sets DONE/FAILED). In tests they are driven explicitly for determinism.
6. **Aggregation (`aggregate_child_results`):** a read-only `SELECT` over children ordered by `created_at`, returning `{parent_id, total_children, done_count, failed_count, pending_count, all_done, children:[{id, goal, status, target_path, artifacts:[{id, path, sha256}]}]}`. `all_done` is true only when there is at least one child and all are `DONE`. No state-machine transition, no write — it never finalizes anyone (ADR-0004 preserved).
7. **Isolation:** each `Orchestrator.run` is its own transaction; a child that fails verification goes to `FAILED` without touching the parent or a DONE sibling. `aggregate` then reports `failed_count>=1, all_done=False`; the parent decides to re-spawn, partially accept, or escalate.
8. **Module API:** `spawn_child_flow(orch, ...)` and `aggregate_child_results(orch, parent)` wrappers are re-exported from `antigona.worker` alongside `SubagentError`/`DepthLimitExceeded`/`BudgetLimitExceeded`.
9. **Known limitation:** no cancel-propagation in P3.1 — cancelling a parent does not cascade to children (documented in ADR-0009, deferred).
10. **Clean-room:** subagent logic borrows nothing from klio-tech/Hermes/OpenClaw/OpenHands — see `docs/adr/0009-subagents-architecture.md`, enforced by `tests/unit/test_clean_room_subagents.py`.


## P3.2 Execution backends (SSH / Modal / Daytona)

P3.2 makes execution backends pluggable through a single workspace abstraction (`BaseWorkspace`). The agent runs tool calls without knowing whether execution takes place locally, in Docker, over SSH, or inside Modal / Daytona serverless containers.

1. **Workspace Interface (`BaseWorkspace`):** Defines `backend_type`, `root_path`, `is_connected`, `connect()`, `write_file()`, `read_file()`, `execute_command()`, and `cleanup()`. `WorkerAgentCore` delegates tool execution exclusively through `self.workspace.*`; direct references to local tool objects (`self.file_tools`, `self.shell_tool`) are removed.
2. **Backend Matrix:**
   - `LocalWorkspace`: local filesystem backend using `WorkspaceGuard` for path-traversal protection.
   - `DockerWorkspace` (mock) / `DockerWorkspaceReal` (real docker daemon adapter).
   - `SSHWorkspace` (mock) / `SSHWorkspaceReal` (real SSH adapter with paramiko).
   - `ModalWorkspace` (mock) / `ModalWorkspaceReal` (real Modal adapter).
   - `DaytonaWorkspace` (mock) / `DaytonaWorkspaceReal` (real Daytona adapter).
3. **Mock / Real Degrade:** Mock backends run safely in CI with zero network calls and zero third-party SDK dependencies. Real backends lazily import SDKs and raise `WorkspaceConnectionError` (subclass of `ToolError`) if SDKs or credentials are not available.
4. **Configuration & Registration:** `WorkspaceFactory.create_workspace(backend=..., workspace_dir=..., config=..., task_id=...)` resolves backends from `Settings` / environment variables (`ANTIGONA_WORKSPACE_BACKEND`, `ANTIGONA_WORKSPACE_MOCK`, backend parameters). Custom backends register via `WorkspaceFactory.register(name, cls)`.
5. **Per-Task Scoping:** Supplying `task_id` scopes the workspace to `<workspace>/<task_id>`. `ws.cleanup()` cleans up task subdirectories or stops remote sandboxes. Subagents inherit the parent flow's workspace backend.
6. **Clean-room:** Execution backends borrow nothing from AGPL klio-tech/Hermes/OpenClaw — see `docs/adr/0010-workspace-backends.md`, enforced by `tests/unit/test_clean_room_backends.py`.


## P3.3 Dual-LLM quarantine model

P3.3 introduces an isolated, tool-less LLM instance (`QuarantineModel`) for sanitizing untrusted inputs (web fetch payloads and untrusted file reads) before they enter the primary agent's reasoning context.

1. **Tool-less Architecture:** `QuarantineModel` has no access to tools or execution capabilities. It receives raw untrusted text and returns a `QuarantineResult` containing safe extracted facts (`safe_facts`) and an injection detection flag (`injection_detected`).
2. **Model Collision Guard:** `quarantine_model` must differ from `model_primary`. If `quarantine_model == model_primary` (when `quarantine_model != "none"`), initialization raises `ModelCollisionError` (mirroring the `LLMJudge` verifier collision guard).
3. **Dual Mock / Real Adapters:** 
   - `MockQuarantineProvider`: Deterministic in-memory provider that strips known injection markers without network access.
   - `HTTPQuarantineProvider`: OpenAI-compatible transport with lazy `urllib` import that handles real external models.
4. **Fail-Closed Transport Strategy:** If the quarantine provider encounters a transport error (`ProviderTransportError` or `ProviderMalformedResponse`), `QuarantineModel.sanitize()` raises `QuarantineUnavailableError`. `WorkerAgentCore` catches this exception, degrades trust (`self._degrade_trust()`), and returns `[QUARANTINE_UNAVAILABLE]`. Raw untrusted bytes NEVER reach the main agent's reasoning context.
5. **Backstop Trust Degradation:** The existing `_degrade_trust()` mechanism and `untrusted_context` shell blocking remain fully operational as backstop security controls.
6. **Clean-room:** Quarantine logic borrows nothing from AGPL klio-tech/Hermes/OpenClaw — see `docs/adr/0011-dual-llm-quarantine-model.md`, enforced by `tests/unit/test_clean_room_quarantine.py`.


## P4.1 Smart egress-proxy + web-fetch

P4.1 introduces a controlled egress transport layer (`EgressProxy`) with deny-by-default allowlist verification, enabling real `WebFetchTool` network fetching while preserving fail-closed security and Dual-LLM Quarantine (P3.3) sanitization.

1. **Egress Transport Layer (`EgressProxy`):**
   - Located in `src/antigona/egress/proxy.py`.
   - `EgressProxy.fetch(url)` serves as the sole network entry point for `WebFetchTool`.
   - Normalizes hostname and checks domain against `Allowlist`.
   - Routes requests via external HTTP proxy (`proxy_url`) or direct urllib HTTP fetch under allowlist control.
   - Raises `EgressUnavailableError` (subclass of `ToolError`) on any allowlist block, invalid URL, or network transport failure.
2. **Deny-by-Default Allowlist (`Allowlist`):**
   - `Allowlist.from_list()` / `from_file()` parses exact hostnames (e.g. `example.com`) and wildcard suffix rules (e.g. `*.example.com`).
   - Suffix rules match subdomains (`api.example.com`) as well as the base domain (`example.com`).
   - `contains(host)` checks normalized hostnames and returns `False` by default if not listed.
3. **Fail-Closed Tool Integration (`WebFetchTool`):**
   - Located in `src/antigona/worker/tools/web_fetch_tool.py`.
   - Replaces the P0 `DisabledWebFetchTool` stub.
   - Delegates all network requests to `EgressProxy.fetch(url)`.
   - Returns `WebFetchResult(url=url, enabled=True, detail=raw_content)` on success.
   - Catches `EgressUnavailableError` and returns `WebFetchResult(url=url, enabled=False, detail="egress blocked: ...")` (fail-closed: raw content is never returned, no open network escape).
4. **Quarantine Model Integration (P3.3):**
   - `WorkerAgentCore._tool_web_fetch()` receives `WebFetchResult`.
   - Passes `result.detail` (the raw webpage text or block message) through `quarantine.sanitize()` before returning data to the agent.
5. **Clean-room & In-Process Boundary:**
   - In-process proxy implementation without container creation via `docker.sock`.
   - Enforced by `tests/unit/test_clean_room_egress.py`.

## P4.2 Micro-VM Isolation

P4.2 introduces hardware-enforced micro-VM isolation (`MicroVMRunner`) for high-risk tool execution.

1. **Micro-VM Runner (`MicroVMRunner`):**
   - Located in `src/antigona/sandbox/microvm.py`.
   - Supports Firecracker (REST API over unix socket) and E2B SDK as micro-VM backends.
   - Configurable via `sandbox_runtime: "docker" | "firecracker" | "e2b"`.
2. **Fail-Closed Availability Check:**
   - `microvm_available(backend)` checks binary presence and `/dev/kvm` accessibility (Firecracker) or SDK and API key availability (E2B).
   - If `sandbox_runtime in {"firecracker", "e2b"}` and the micro-VM runtime is unavailable, execution of high-risk commands raises `MicroVMUnavailableError` (fail-closed, loud WARN).
   - Host execution fallback is strictly prohibited for high-risk commands when micro-VM runtime is selected.
3. **Shell Tool Routing (`WorkspaceShellTool`):**
   - Located in `src/antigona/worker/tools/shell_tool.py`.
   - Low-risk allowlist commands (P0 binaries without `/`) execute directly on host inside workspace.
   - High-risk commands route to `MicroVMRunner.spawn()` / `exec()` / `teardown()`.
4. **Security Invariants:**
   - `docker.sock` is never exposed or mounted into guest micro-VMs.
   - Guest VM network traffic routes through `EgressProxy` (P4.1) for deny-by-default domain filtering.
   - Output from micro-VM executions is marked `untrusted=True`.
5. **Clean-room & Boundaries:**
   - Enforced by `tests/unit/test_clean_room_microvm.py`. See `docs/adr/0013-micro-vm-isolation.md`.





## P4.3 Storage: Postgres + Redis

P4.3 moves the durable layer onto **async SQLAlchemy** with PostgreSQL as the
primary store and SQLite (aiosqlite) as the default fallback, and adds Redis as a
wake-up transport and a state cache. See `docs/adr/0014-postgres-redis.md`.

```
        Gateway / Orchestrator / Worker / Verifier
                      │ (async)
                      ▼
          storage.session.get_session()        ← async_sessionmaker(expire_on_commit=False)
                      ▼
          storage.engine.build_async_engine()  ← create_async_engine
    ┌─────────────────┴──────────────────┐
    ▼                                    ▼
 postgresql+asyncpg://…            sqlite+aiosqlite://…  (default fallback)
 (Alembic 0001 + PL/pgSQL triggers) (create_all + PRAGMA + RAISE(ABORT) triggers)

    Redis (optional, ANTIGONA_REDIS_URL)
    ├── antigona:queue:{lane} — wake-up signal (RPUSH → BLPOP)
    └── state:{task_id}       — state cache with TTL
```

1. **Async storage package (`src/antigona/storage/`):**
   - `engine.py` — `build_async_engine()`, URL normalization onto async drivers
     (`sqlite://` → `sqlite+aiosqlite://`, `postgresql://` → `postgresql+asyncpg://`),
     slow-query instrumentation mirrored from the sync layer, SQLite PRAGMA
     (`foreign_keys`, WAL), password masking for every logged URL,
     `connect_with_retries()` / `ensure_storage_available()` (fail-closed gate),
     `create_all()` and the dialect-specific append-only DDL.
   - `models.py` — re-export of `antigona.models`; **one** `Base.metadata` for the
     sync layer, the async layer and Alembic, so schema drift is impossible.
   - `session.py` — `create_session_factory()` and the `get_session()` async
     context manager.
   - `migrations/` + `migrator.py` — async Alembic environment, `0001_initial`
     (schema v6 + append-only triggers), `antigona db upgrade`.
2. **Task brokers (`src/antigona/queue/redis_broker.py`):**
   - `TaskBroker` protocol (`enqueue`/`dequeue`/`ack`/`nack`/`close` plus the sync
     `signal`/`wait` bridge), `RedisTaskBroker` (lazy `redis.asyncio` import),
     `InMemoryBroker` (CI/mocks, no sockets), `NullBroker` (`redis_url=None`).
   - `DurableQueue` remains authoritative: it signals the broker only **after** a
     successful commit, and a woken worker still has to win `claim()` (lease +
     revision CAS) in SQL. A lost signal costs latency, never a task.
3. **State cache (`src/antigona/durable/state_cache.py`):**
   - `StateCache.stage()` buffers the new state inside the transition's unit of
     work and publishes it from an `after_commit` hook; a rollback invalidates
     instead of publishing.
   - `read_state()` is cache-aside: cache → miss/error → SQL → refill. The
     database is always the arbiter; the TTL bounds any divergence.
   - `TaskRepository(session, state_cache)` and `DurableQueue(session, broker,
     state_cache)` accept it optionally; `None` reproduces pre-P4.3 behaviour.
4. **Fail-closed / fail-soft:**
   - Postgres unreachable at startup → retries with exponential backoff
     (`db_connect_retries` / `db_connect_backoff_seconds`), then
     `StorageUnavailableError` aborts the worker. No open-loop operation.
   - Redis unreachable → one `queue.broker.degraded` / `state_cache.degraded`
     warning, then broker and cache turn into no-ops and the pure SQL path
     (polling + SQL reads) carries the full functionality.
5. **Invariants and clean-room:**
   - `DONE` stays Verifier-only; `state_transitions` stays append-only in both
     dialects; SQLite remains the default so P0–P4.2 behaviour is unchanged.
   - Enforced by `tests/unit/test_storage.py`, `tests/unit/test_redis_broker.py`,
     `tests/unit/test_state_cache.py`, `tests/unit/test_clean_room_storage.py`,
     `tests/integration/test_storage_pipeline.py`, and (under
     `ANTIGONA_TEST_POSTGRES_URL` only) `tests/integration/test_postgres_storage.py`.

## P5.1 Delivery channels (Discord, Slack, WhatsApp, Signal, Email)

P5.1 expands outgoing notifications into multi-channel delivery (`discord`, `slack`, `whatsapp`, `signal`, `email`) while preserving the existing outbox state engine and backward compatibility. See `docs/adr/0015-delivery-adapters.md`.

```
   Producers (repository.py / verifier_service.py / cron.py)
        │ session.add(DeliveryOutbox(adapter=<channel>, payload=...))
        ▼
   delivery_outbox (Postgres/SQLite — durable truth)
        │ DeliveryWorker.claim() -> CAS lease
        ▼
   router.deliver(item)  (reads item.adapter)
        │ get_adapter(item.adapter, settings)
        ▼
   DeliveryAdapter Protocol (deliver(event, idempotency_key))
   ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼          ▼          ▼          ▼          ▼
 telegram   discord    slack    whatsapp    signal     email     progress    fake
```

1. **Unified Adapter Contract (`antigona.delivery.adapter`):**
   - `DeliveryAdapter` protocol: `name: str` and `deliver(event: ProgressEvent, idempotency_key: str) -> None`.
   - `ProgressEvent` dataclass: `task_id, session_id, correlation_id, step_id, status, message`.
   - Adapters handle output formatting and transport dispatch without modifying task state.

2. **Factory & Router (`antigona.delivery.factory`, `antigona.delivery.router`):**
   - `get_adapter(channel_name, settings)` resolves configured adapters. Unknown channel name raises `UnknownChannelError` (no silent fallback).
   - `adapter="progress"` maps to `settings.delivery_default_channel` (default: `"telegram"`).
   - `Router(settings)` caches adapter instances per process lifetime.

3. **Dual Mock / Real Runtime & Lazy Imports (`antigona.delivery.adapters.*`):**
   - Each channel adapter (`discord`, `slack`, `whatsapp`, `signal`, `email`) supports `delivery_mock=True` (or missing credentials) to run safely without network access.
   - External SDK / transport libraries are lazily imported inside `deliver()`/`connect()`.

4. **Fail-Closed & Fail-Safe Delivery:**
   - Exception during delivery retains outbox item as `PENDING`, updates `attempts`, sets exponential backoff `available_at`, and records `last_error`. No dropped messages or false `DELIVERED` status.
   - `DONE` remains strictly Verifier-only.

5. **Clean-Room & Invariants:**
   - Enforced by `tests/unit/test_delivery_adapters.py`, `tests/integration/test_delivery_router.py`, and `tests/unit/test_clean_room_delivery.py`.
