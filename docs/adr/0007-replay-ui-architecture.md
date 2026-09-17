# ADR-0007: Replay-UI architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Deciders:** Architecture Board
- **References:** ADR-0004 (Verifier boundary), ADR-0005 (Skills), ADR-0006 (Cron)

## Context

Antigona records every flow transition as an append-only `StateTransition` journal in SQLite.
P0/P1/P2 transitions leave a complete audit trail, but there is no structured way to
**read back** a flow's trajectory for debugging, observability, or replay.

P2.3 introduces a read-only replay layer that surfaces this journal:

1. A flow's goal, status, revision, and timestamps.
2. Every state transition (task and step) — with actor, reason, and timestamp.
3. Every step executed — input, output, retry count.
4. Every artifact produced — path, hash, size, verification status.

## Decision

### ReplayEngine — read-only layer

`ReplayEngine` (in `src/antigona/replay.py`) is a **read-only** service that:

- Executes plain SQLAlchemy `SELECT` queries against `state_transitions`, `flow_steps`,
  `artifacts`, and `task_flows`.
- Never calls `repository.transition()`, never writes `StateTransition`, never flushes
  a session. Replay is an observer, not a participant.
- Returns dataclass-typed results (`ReplayTrajectory`, `TransitionReplay`, `StepReplay`).
- Enforces owner isolation: when `owner_id` is passed, a mismatch produces
  `ReplayTaskNotFound` (HTTP 404), not 403 — indistinguishable from "flow does not exist".
- Supports filtering by `actor`, `entity_type`, and `created_at` range.
- Provides `get_timeline()` — a flat chronological merge of all record types.
- Provides `render_replay_text()` with Rich formatting (Panel, Table, Tree).

### Output format

| Format | Consumer | Component |
|---|---|---|
| JSON (dataclass `to_dict()`) | REST API consumer | Gateway endpoint |
| Rich CLI (Panel/Table/Tree) | Operator terminal | `antigona replay` CLI |
| Rich DataTable | Textual TUI | TUI replay panel |

The dataclass model (`StepReplay`, `TransitionReplay`, `ReplayTrajectory`) is the
canonical internal representation. JSON and Rich are derived views.

### Owner isolation

- `ReplayEngine.get_trajectory()` accepts optional `owner_id`.
- If `owner_id` is provided and does not match `flow.owner_id`, raise `ReplayTaskNotFound`.
- The Gateway endpoint passes the authenticated `owner_id` from the bearer token.
- 404 (not 403) prevents information leakage about the existence of flows owned by others.

### Verifier boundary

- ReplayEngine has **no** reference to the Verifier, VerifierClient, or verifier credential.
- ReplayEngine imports no state machine, no `transition()`, no `DONE` mutation path.
- The Gateway replay endpoint imports only `ReplayEngine` from `replay.py`;
  it does not import `repository.transition()` or any verifier module.
- These constraints are verified by:
  - `test_clean_room_replay.py` (no upstream tracer imports)
  - `grep -rn "autoincrement\|transition\|DONE\|commit\|flush" src/antigona/replay.py`

### Dependencies

- `rich` (already in dependencies) — CLI formatting.
- `textual` (already in dev dependencies) — TUI.
- No `graphviz`, `matplotlib`, `plotly`, `pyvis`, `d3.js`, or any graph visualizer.
- No upstream tracing libraries (celery.result, temporal, prefect, langsmith).

## Consequences

1. Replay is read-only and append-only journal is never mutated.
2. Owner isolation uses 404 (opaque) rather than 403 (information-leaking).
3. All formatting is additive — the dataclass model stays clean.
4. P2.4 may add live replay via WebSocket push; P2.3 explicitly defers that.
5. Graph visualization (Mermaid, Graphviz) is postponed to P5.
