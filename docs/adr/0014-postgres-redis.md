# ADR-0014: Async Postgres + Redis for the durable layer (P4.3)

- **Date:** 2026-07-26
- **Status:** Accepted
- **Phase:** P4 «Изоляция + масштаб», step P4.3
- **Supersedes:** nothing. **Extends:** P0 durable layer (`database.py`, `queue.py`, `durable/state_machine.py`)
- **Related:** ADR-0001 (OpenHands SDK boundary), ADR-0003 (pgvector memory), ADR-0004 (Verifier boundary)

## Context

The P0 durable layer is bound to **synchronous SQLite**: `Database` builds a sync
engine, `DurableQueue` polls `queue_jobs` on a timer, and the state machine reads
every task state straight from the database. Three limits follow from that:

1. **No managed schema evolution.** The schema version is a hand-maintained
   integer plus `migrations/*.sql`; nothing verifies that a deployed database
   matches the ORM metadata.
2. **Polling-only queue.** A worker with no work sleeps and retries; wake-up
   latency is a floor set by the poll interval, and it does not amortise across
   many workers.
3. **Every state read is a query.** Hot read paths (gateway status, TUI) pay a
   database round trip even when the answer changed seconds ago.

P4 requires scale. Scale requires Postgres as the primary store, a broker for
wake-ups, and a cache for hot reads — without weakening any invariant that the
earlier phases established.

## Decision

### 1. Async SQLAlchemy, two dialects, SQLite stays the default

A new package `antigona.storage` builds `create_async_engine` for exactly two
drivers: `postgresql+asyncpg` (primary) and `sqlite+aiosqlite` (fallback and
**default**). `normalize_db_url` rewrites legacy URLs onto their async driver
(`sqlite://` → `sqlite+aiosqlite://`, `postgresql://` → `postgresql+asyncpg://`),
so an existing `ANTIGONA_DATABASE_URL` keeps working untouched. Any other driver
is refused at configuration time (`UnsupportedDatabaseURL`) rather than failing
obscurely at first connect.

There is **one** `Base.metadata` — the async layer re-exports the ORM models from
`antigona.models` instead of defining its own. Two model sets could drift; one
cannot.

### 2. Alembic is the only way a Postgres schema evolves

`storage/migrations/` holds an async `env.py` (adapted from the official
`alembic init -t async` template, MIT) and `0001_initial`, which emits the schema
from the shared metadata (equivalent to `SCHEMA_VERSION = 6`) and then installs
the append-only guards for the dialect at hand. `antigona db upgrade` wraps
`alembic upgrade head`.

For the SQLite fallback, `create_all()` remains sanctioned — it is what P0 does
today, and the migration emits the same metadata, so the two paths converge.
`schema_version` is kept for the sync layer; `alembic_version` is the async
layer's marker.

### 3. Redis is a transport and a cache — never the source of truth

The SQL row is the truth. Concretely:

- **Queue.** `DurableQueue` (lease + revision CAS on `queue_jobs`) decides who
  owns a task, exactly as before. `RedisTaskBroker` only carries the signal
  "there is work now" (`RPUSH`), so a worker can block on `BLPOP` instead of
  polling. A woken worker still has to win `claim()` in SQL. A lost message costs
  latency, never a task; a duplicated message is resolved by the CAS.
- **State cache.** After a transition **commits**, the new state is published to
  `state:{task_id}` with a TTL. Reads are cache-aside: miss, expiry or cache
  outage all fall through to SQL, which then refills the cache. Writes never
  travel through the cache — the transition graph, the revision CAS and the
  append-only journal all stay inside the SQL transaction.

`redis_url = None` (the default) yields `NullBroker` and a disabled cache, i.e.
behaviour byte-for-byte identical to P4.2.

### 4. Fail-closed on Postgres, fail-soft on Redis

| Failure | Behaviour |
|---|---|
| Postgres unreachable at startup | `connect_with_retries` retries `db_connect_retries` times with exponential backoff, then raises `StorageUnavailableError`; the worker aborts startup instead of running without durable state |
| Postgres fails mid-task | The transaction fails, the task goes to `FAILED` through the normal worker cycle and is journalled; no partial state |
| Redis unreachable | One `queue.broker.degraded` / `state_cache.degraded` warning, then broker and cache become no-ops: pure SQL polling and SQL reads. Full functionality, degraded latency |
| Cache disagrees with the database | The database wins; the cache is overwritten or invalidated, and the TTL bounds the divergence window |

The asymmetry is deliberate: losing the durable layer loses state, so it must
stop the process; losing Redis loses only speed, so it must not.

### 5. The sync layer is deprecated, not deleted

`antigona.database` keeps working for every legacy caller. Removing it is a
separate refactor step with its own migration of call sites; silently dropping it
here would risk data paths that no test covers yet.

## Invariants preserved

1. **Only the Verifier sets `DONE`.** `VERIFIER_ONLY_TRANSITIONS` is unchanged,
   and neither broker nor cache offers a way around it — both sit downstream of
   the graph check (`tests/unit/test_state_cache.py::test_cache_never_opens_a_path_to_done`).
2. **The journal is append-only** in both dialects: `RAISE(ABORT)` triggers on
   SQLite, `RAISE EXCEPTION` triggers on Postgres, installed by migration 0001.
3. **Backward compatibility.** Default stays SQLite; `from antigona.queue import
   DurableQueue` still resolves after `queue.py` became a package; P0–P4.2 tests
   stay green.
4. **Clean-room.** Original API and naming; no AGPL third-party storage or queue
   code was read or adapted. Verified by `tests/unit/test_clean_room_storage.py`.
5. **No credential leakage.** Database URLs are masked (`user:[REDACTED]@host`)
   before they reach any observability event.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Redis Streams as the queue of record (consumer groups, XACK) | Makes Redis authoritative for task ownership; a Redis restart could lose or double-run tasks. SQL CAS already solves the race, and it is durable |
| Drop `database.py` and port every caller now | Large blast radius inside one step; the sync layer is still exercised by most of the suite. Deprecation path instead |
| Hand-written `0001_initial` with explicit `op.create_table` calls | A second transcription of the schema that can silently drift from the ORM metadata |
| testcontainers / docker-compose Postgres in CI | Out of scope for P4.3 and forbidden by the step's rules: CI runs on aiosqlite + mocks; real Postgres/Redis run only under `ANTIGONA_TEST_POSTGRES_URL` / `ANTIGONA_TEST_REDIS_URL` |
| Write-through cache (publish before commit) | Would expose a state that the transaction may still roll back — the cache could then disagree with the journal |

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `ANTIGONA_DB_URL` | derived from `ANTIGONA_DATABASE_URL` | Async database URL |
| `ANTIGONA_DATABASE_URL` | `sqlite:///./antigona.db` | Legacy sync URL, still used by `database.py` |
| `ANTIGONA_REDIS_URL` | unset → Redis off | Broker + state cache endpoint |
| `ANTIGONA_REDIS_STATE_TTL` | `300` | State-cache TTL, seconds |
| `ANTIGONA_DB_CONNECT_RETRIES` | `5` | Startup connect attempts before failing closed |
| `ANTIGONA_DB_CONNECT_BACKOFF` | `1.0` | First backoff delay, doubled per attempt |

## Consequences

- **Positive:** managed migrations; Postgres-ready durable layer; sub-poll wake-up
  latency when Redis is present; cheaper hot reads; one metadata object for both
  layers; explicit fail-closed startup gate.
- **Negative:** two storage layers coexist until the sync one is retired; Alembic
  and aiosqlite become hard dependencies; the Redis sync bridge needs a private
  event loop for sync callers (documented in `redis_broker.py`).
- **Neutral:** `asyncpg` and `redis` stay lazily imported, so a default SQLite
  deployment never loads them.
