# CLI boundary — Gateway thin-client model

**Status:** authoritative (ADR-0017) · **Date:** 2026-08-09
**Sources:** `ARCHITECTURE.md` §2, `docs/adr/0014-postgres-redis.md`,
`docs/adr/0016-conversation-public-taskflow-internal.md`, `docs/adr/0017-ui-client-boundary-gateway.md`,
truth-audit of the rejected bypass draft (user evidence package, 2026-08-08/09).

## Ten norms

1. **CLI and Telegram are thin presenters.** They do not own business state,
   memory, task execution, provider selection or completion.
2. **The Gateway is the single user-facing control-plane boundary.** Commands,
   dialogue turns, approvals, sessions, history, memory projections, events and
   results all go through it.
3. **UI transport is HTTP + WebSocket/polling.** Reconnect uses a monotonic
   event sequence (`GET /events?after_seq=`) plus WS with the same cursor,
   bounded exponential backoff and dedupe by `(flow_id, seq)`. Redis Pub/Sub is
   not an UI transport.
4. **Durability lives server-side.** Local wait cancellation never cancels the
   flow; remote cancel is an explicit Gateway call; a final authoritative read
   precedes any local-cancellation outcome.
5. **Local state is a non-authoritative UI/reconnect cache only** (cursor,
   menu, theme, scroll, animation, reconnect/backoff). It can never finalize a
   task or replace a Gateway read.
6. **PostgreSQL/SQLite and Redis are hidden behind Core/Worker/storage.**
   SQL is the truth; Redis is an optional wake-up broker and cache. UI packages
   do not connect to them (P10, `tests/architecture/test_ui_boundary.py`).
7. **Business command SSOT is the Gateway registry** (`core/command_registry.py`,
   `GET /commands`). Local UI commands are separate and explicitly local.
   `contracts/commands.json` is a drift-checked projection, never an equal
   second registry.
8. **The Verifier defines success.** The CLI renders validated terminal results
   and never reports `DONE` from its own assumptions
   (`cli_ui/flow_adapter.py`, `TerminalOutcome`).
9. **Presentation stays transport-specific.** CLI layout and Telegram inline
   keyboards differ; the shared layer is DTO/API semantics, not presentation
   code.
10. **Machine-readable output is a separate contract** (future ADR): versioned
    JSON schema per command, stdout = data only, stderr = diagnostics, stable
    exit codes, redaction, parser tests. Until then `--json` exists only where
    implemented (`antigona replay`).

## Enforced by tests

- `tests/architecture/test_ui_boundary.py` — UI packages must not import
  `database`, `repository`, ORM `models`, `storage`, `durable`, `worker`,
  `agent`, `verifier`, `providers`, `redis` (P10).
- `tests/architecture/test_command_parity.py` — every Gateway business command
  (cli channel) is present in the CLI catalog; `contracts/commands.json` is a
  drift-checked projection of the registry.
- `tests/unit/test_commands_contract.py` — fixture-level checks for
  `contracts/commands.json` (shape, drift-sync with registry).

## Diagnostic read-only access — design sketch (future, not implemented)

If an offline/diagnostic mode is ever needed, it must be **authorized Gateway
endpoints**, not direct DB reads:

- `GET /api/v1/diagnostics/sessions` — owner-scoped session list (reads the
  sessions DB behind the Gateway).
- `GET /api/v1/diagnostics/sessions/{id}/history` — owner-scoped message
  projection with redaction.
- `GET /api/v1/diagnostics/flows` / `GET /api/v1/diagnostics/flows/{id}` —
  owner-scoped flow/step/artifact projections.
- All endpoints: `Authorization: Bearer <token>` checked against
  `ANTIGONA_DEV_TOKENS`; owner filtering mandatory; redaction per
  `schemas.py` `_public_*` helpers; no new transport beyond Gateway HTTP/WS.

Design rule: any gap a client hits must be closed with a narrow, backward-
compatible Gateway endpoint/DTO — never by giving the client direct access to
the infrastructure.

## Bypass draft — disposition

The rejected bypass draft (direct Postgres/Redis CLI, Redis handshake, offline
DB read mode, ORM-as-UI-contract) is archived under
`scratch/bypass-archive-20260809/` and must not be resurrected without an
official architecture change (repeal of `ARCHITECTURE.md` §2,
revision of ADR-0014, separate auth and threat model).
