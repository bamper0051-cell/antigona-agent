# ADR-0008: TUI architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Deciders:** Architecture Board
- **References:** ADR-0004 (Verifier boundary), ADR-0006 (Cron), ADR-0007 (Replay-UI)

## Context

Up to P2.3 the only interactive surface over a flow was the CLI: `antigona run`
attaches a WebSocket stream, `antigona replay` prints a trajectory, `antigona approve`
decides a single approval by id. Every one of those needs an id the operator must
already know, and none of them shows *what is waiting for them right now*.

`src/antigona/tui.py` existed as a draft Textual screen — a flat row of buttons over one
`DataTable` and a `Log`. It could attach, cancel, replay and decide, but only for an id
typed by hand: there was no list of flows, no list of pending approvals, and no live view.

P2.4 turns that draft into the operator console: four tabs (Flows / Live / Approvals /
Replay), backed by three new read-only Gateway endpoints.

## Decision

### 1. The TUI is a thin client. All authority stays in the Gateway.

`AntigonaApp` talks to the Gateway over HTTP and WebSocket through `GatewayClient` and
holds nothing else: no `Session`, no `TaskRepository`, no `Database`, no verifier
credential. It cannot import them — `tests/unit/test_clean_room_tui.py` asserts that the
module's only relative import is `from .cli import GatewayClient`.

Consequences that follow from that single rule:

- **The TUI cannot finalize a flow.** There is no code path to the Verifier-owned terminal
  state, consistent with ADR-0004. The module does not even spell that state: its status
  palette is keyed on lowercase strings, so a grep for the upper-case name over `tui.py`
  comes back empty.
- **Owner isolation is not a client concern.** The TUI renders whatever the Gateway
  returns; the Gateway scopes every list to the bearer token's owner.
- **Approve / reject / cancel reuse the existing endpoints** —
  `POST /approvals/{id}/decision` and `POST /flows/{id}/cancel` — the same ones the CLI
  calls. P2.4 adds no new write path anywhere.

### 2. Four tabs over `Tabs` + `ContentSwitcher`, not four screens.

| Tab | Source | Widget |
|---|---|---|
| Flows | `GET /flows` | `DataTable` — `ID │ Goal │ Status │ Rev │ Created` |
| Live | `WS /flows/{id}/progress` | `Static` summary + `DataTable` — `# │ From -> To │ Actor │ Reason │ Time` |
| Approvals | `GET /approvals?status=PENDING` | `DataTable` — `ID │ Flow │ Tool │ Risk │ Reason` + Approve/Reject |
| Replay | `GET /flows/{id}/replay` | nested tabs: transitions `DataTable`, steps `Tree`, artifacts `DataTable` |

A `Tabs` bar drives a `ContentSwitcher` whose panes are ordinary containers, so all four
panes exist in the DOM at once and keep their state across switches. Tab ids (`tab-flows`)
and pane ids (`pane-flows`) are kept distinct and mapped through one module-level table
(`TAB_SPECS`), which is also what the tests assert against. A single shared `Log` sits
below the switcher: one event log for the whole session, not one per tab.

Keyboard: `1`–`4` select tabs, `r` refreshes, `a` approves, `x`/`d` rejects, `q` quits.

### 3. Rendering is split into pure projections plus a thin widget layer.

`flow_rows`, `approval_rows`, `transition_row(s)`, `step_labels`, `artifact_rows`,
`status_color`, `risk_color` are module-level functions over plain dicts. They take no
widget and touch no app state, so table content is unit-testable without a running app;
the widget layer only adds colour (`rich.text.Text`) and row keys. Row keys are the flow
and approval ids, which is how a selection maps back to an API call.

### 4. The live stream is one cancellable Textual worker.

`stream_progress_ws(flow_id)` on `GatewayClient` is an async generator over the existing
`WS /flows/{id}/progress` socket: same protocol as `_stream_ws`, but it yields parsed
dicts instead of printing, and returns after the `end` or `error` frame the gateway sends
before closing. The TUI drives it from `run_worker(..., exclusive=True, group="stream")`,
so selecting another flow cancels the previous socket, and `on_unmount` cancels the last
one. Connection failures degrade to a red `stream: disconnected` label plus a `Reconnect`
button — never a crash, since every worker runs with `exit_on_error=False`.

Polling `GET /flows` on a timer stays as the fallback for flows the operator has not
opened; the interval is a constructor argument (`refresh_interval`, `0` disables it,
which is what tests use).

### 5. Three new Gateway endpoints, all read-only.

- `GET /flows?status=&limit=&offset=` → `FlowListView` — `select(TaskFlow).where(owner_id == ...)`.
- `GET /approvals?status=PENDING&limit=&offset=` → `ApprovalListView` — `Approval` joined to
  `TaskFlow` on `owner_id`, so an approval is visible only to the owner of the flow that
  raised it.
- `GET /approvals/{id}` → the existing `ApprovalView`, guarded by the same `load()` helper
  the decision endpoint uses.

All three are `SELECT`-only: no `repository.transition()`, no enqueue, no writes, so
`tests/integration/test_p0_boundaries.py` (ORM ↔ SQL parity) is unaffected. Each emits a
`log_event` (`gateway.flows_listed`, `gateway.approvals_listed`, `gateway.approval_read`)
carrying the correlation id. `limit` is clamped to `MAX_PAGE_SIZE = 200` so a listing
cannot be turned into a full-table dump.

**404, never 403** (inherited from ADR-0007): a foreign approval id is indistinguishable
from a missing one, so the endpoint is not an existence oracle.

### 6. Entry point: `antigona tui`.

`antigona tui [--gateway URL] [--token T] [--refresh SECONDS]` is a plain Typer command
that constructs `AntigonaApp` and calls `.run()`. The `antigona-tui` console script and
`python -m antigona.tui` keep working through `main()`. Importing the module starts
nothing — `__all__ = ["AntigonaApp", "main"]`, and construction performs no IO, which is
what makes headless `run_test()` possible.

### 7. Deliberate deviation from the plan: the replay status filter is client-side.

P2_4_PLAN.md §2.6 suggests wiring `#replay_status_filter` into `get_replay(entity_type=...)`.
The replay endpoint has no status filter — `entity_type` selects `task` vs `step`, which is
a different axis. Rather than mislabel one as the other, `#replay_actor_filter` is passed to
the API as `actor`, and `#replay_status_filter` narrows the returned transitions by target
state locally (`transition_rows(payload, status_filter)`). No new endpoint parameter was
invented for a filter the projection can do for free.

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| `TabbedContent` / `TabPane` instead of `Tabs` + `ContentSwitcher` | Its `with`-block compose form needs an active app context, so the DOM could not be built or asserted outside a running app. |
| Switch the live stream to SSE | Explicitly out of scope (§1.3); the WS endpoint already exists and is exercised by the CLI. |
| Let the TUI read SQLite directly for the flow list | Would duplicate owner-isolation logic outside the Gateway and give the client a session handle. Rejected on the strength of rule 1. |
| Create flows from the TUI | Out of scope (§1.3); creation stays in `antigona run`. |
| Graph visualization of transitions (graphviz/mermaid) | Out of scope; the TUI is tabular and tree-based (same line as ADR-0007). |

## Consequences

- The operator can see every flow they own, every approval waiting on them, and the live
  transition stream, without knowing any id in advance.
- `GatewayClient` gained an optional `transport` argument. It defaults to `None` (a normal
  socket client), and lets tests point the real client at an in-process ASGI app — the TUI
  boundary tests drive real buttons against the real Gateway with no mock in between.
- Three more read endpoints widen the Gateway's read surface; each is owner-scoped,
  paginated and logged.

## Clean-room

- Allowed: `textual` (MIT), `rich`, `websockets`, `httpx` — all pre-existing dependencies.
  P2.4 adds none.
- Forbidden and asserted absent from `tui.py` / `cli.py` by
  `tests/unit/test_clean_room_tui.py`: any upstream agent shell (Hermes, OpenClaw,
  OpenHands, klio-tech) and any other TUI toolkit (`prompt_toolkit`, `urwid`, `npyscreen`,
  `py_cui`, `blessed`, raw `curses`), plus `electron` / `graphviz`.
- The tab topology, column sets and colour mapping are original to this repository; no
  layout was transcribed from another agent console.
- AGPL klio-tech remains unread and unborrowed (`docs/THIRD_PARTY_STRATEGY.md`).
