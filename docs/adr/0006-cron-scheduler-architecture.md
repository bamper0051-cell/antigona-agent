# ADR-0006: Cron scheduler architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Step:** P2.2 (sub-step P2.2.a), see `docs/ROADMAP.md`

## Context

P2.2 gives Antigona a durable cron scheduler on top of `queue_jobs`. The scheduler must:

1. Create repeating schedules by standard 5-field cron expression.
2. On tick, produce `TaskFlow` via `TaskRepository` + `DurableQueue` — the standard task pipeline.
3. Support **sticky cancel**: a cancelled schedule never produces tasks, and cancellation survives restarts.
4. Use **idempotency keys** per cron tick, so a duplicate tick on the same minute does not duplicate tasks.
5. Provide API (Gateway REST + CLI) for schedule management: CRUD + listing.
6. Keep a minimal journal of lifecycle events per schedule (creation, tick error, cancellation) — without a full state machine (cron schedules do not pass DONE/VERIFYING).

## Decision

### 1. CronSchedule is not a state machine

Unlike `task_flows`, cron schedules have no Verifier lifecycle. They are **enabled / disabled / cancelled** — boolean flags, not a state machine. There is no `DONE`/`VERIFYING` path. Sticky cancel (`cancelled=true`, `enabled=false`) is an owner operation, not a verification one.

**Consequence:** Gateway/CLI code must never import `verifier` or `verifier_service`. Schedules do not pass `ACTIVE`/`DONE`.

### 2. Tick mechanism: BackgroundTask in Gateway

FastAPI `lifespan` starts an asyncio task that every N seconds (configurable via `Settings.cron_tick_interval_seconds`, default 30) calls `CronScheduler.tick()`. Tick takes a write-lock via the SQLite session (serializable). Graceful shutdown: lifespan awaits the current tick's completion.

**Rationale:** Simplicity, no separate process. At P2.2 scale (single Gateway, SQLite) this is sufficient. An external poller (`antigona-cron-ticker`) is documented as the alternative for P4+.

**Safety:**
- `tick()` uses its own SQLAlchemy session, committing only after all due schedules are processed.
- If one schedule errors, its error is journaled (`schedule_events`, `event_type='errored'`), but tick continues processing other schedules.

### 3. Owner isolation

All Gateway REST endpoints check bearer token → `owner_id` (same pattern as flows/skills). `create_schedule`, `cancel_schedule`, `list_schedules`, `get_schedule`, `get_jobs` filter by `owner_id`. Gateway has no path to modify or cancel another owner's schedule.

### 4. Sticky cancel as the only termination mechanism

No `DELETE` endpoint. `cancel_schedule` sets `cancelled=true, enabled=false` — irreversible. This mirrors the `CANCELLED`-is-sticky rule from task flows.

### 5. Journal `schedule_events` as append-only table

An append-only table `schedule_events` tracks lifecycle events: `created`, `ticked`, `errored`, `cancelled`. This is the only audit trail; Verifier may read it but does not participate in schedule transitions.

### 6. Durable layer stays on SQLite until P4

Cron schedules and events live in the same SQLite database next to `task_flows`, `queue_jobs`. Migration to PostgreSQL is deferred to P4.

## Consequences

- `CronScheduler` constructor accepts a `Session` and uses `croniter` (MIT, already a dependency) for expression validation and `next_run_at` computation.
- `tick()` builds idempotency key as `cron:{schedule_id}:{next_run_at.isoformat()}` to prevent duplicate task creation.
- `schedule_events` is append-only via SQLite triggers (same pattern as `state_transitions`).
- Gateway REST gets 5 new endpoints (no `DELETE`).
- CLI gets `antigona cron create|list|show|jobs|cancel|tick` subcommands.
- No new dependencies beyond `croniter` (already present).

## Clean-room restrictions

- **No import of upstream cron libraries** (`celery.beat`, `apscheduler`, `schedule`). `CronScheduler` is self-authored using only `croniter` (MIT, already a dependency) for expression parsing. Verified by `tests/unit/test_clean_room_cron.py`.
- Cron record format (§6 of P2.2 plan) is original — does not copy Celery Beat `PeriodicTask` or APScheduler `Job` schemas.
- AGPL klio-tech sources are not read or borrowed.

## References

- `docs/ROADMAP.md` — P2.2 plan (§1 scope, §2 architecture, §4 acceptance criteria).
- `docs/adr/0004-p1-verifier-boundary.md` — Verifier boundary (cron schedules do not cross it).
- `docs/adr/0005-skills-clean-room-format.md` — skills registry ADR (same owner-isolation pattern).
- `docs/THIRD_PARTY_STRATEGY.md` — third-party code rules.
