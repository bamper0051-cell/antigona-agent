# ADR-0016 — Conversation is the public model; TaskFlow stays internal execution

- Status: **Accepted**
- Date: 2026-08-02
- Deciders: Owner (master prompt «ANTIGONA LIVE AGENT CLI RESTORATION»), Hermes (temporary lead architect)
- Replaces: implicit «CLI = TaskFlow dispatcher» contract (pre-P5.4)

## Context

The CLI exposes TaskFlow directly to the user: submit → QUEUED → manual `/status`, `/approve <id>`.
TaskFlow is an execution-layer concern and must not be the user-facing interaction model.
The canonical conversation plumbing already exists (`input_pipeline.process_user_input`,
`IntentRouter`, `ConversationEngine`, sessions, gateway events) and is used by Telegram,
but the CLI bypasses it.

## Decision

1. **Conversation/Turn is the public interaction model.** Every CLI user input is a *turn*
   processed by the canonical pipeline: normalise → resolve context → classify intent →
   route (conversation / task / steering / approval / control) → delegate to Gateway →
   return a semantic outcome (`ProcessingOutcome`) + human `response_text`.
2. **TaskFlow remains the internal execution layer.** The CLI never creates, mutates or
   declares terminal states itself; it only submits/steers/cancels through the Gateway and
   waits for the canonical success gate (`GatewayClient.wait_for_terminal`, Verifier-only DONE).
3. **Natural-language approval** (`да` / `разрешаю` / `нет`) is resolved by the CLI turn layer
   to the pending approval of the active flow and sent to `POST /approvals/{id}/decision`;
   slash commands (`/approve <id>`) remain the explicit fallback.
4. **No second core, no local authority.** The CLI remains a client: no local task DB,
   no local Verifier, no local DONE, no independent tool providers, no bypass of Gateway
   or approval gates.
5. **Chit-chat must not create a TaskFlow** (`conversation.ask` → `CONVERSATION_FINAL`).
6. **Worker "thinking"** is enabled by routing generic tasks to the existing TurnEngine path
   (`should_use_turn_worker`) when a provider key is configured — this is a worker routing
   decision, not a new subsystem.
7. **The CLI turn layer is a thin client-side adapter**, composed of the canonical components
   (`IntentRouter` for classification, `ConversationEngine`/`chitchat_reply` for chit-chat,
   `GatewayClient` for every mutation). It is **not** a re-implementation of the server-side
   `input_pipeline`: the CLI must not read the durable DB, so `ContextResolver` (which binds
   tasks via the local DB) stays server-side; the CLI binds context via its own active-flow
   state. Natural approval ("да"/"нет") maps to `POST /approvals/{id}/decision`.

## Consequences

- CLI UX: natural language turns, no IDs required for approvals, verified results rendered
  automatically, chit-chat answered without flows.
- Architecture invariants (single Core, single Gateway, Verifier-only DONE, no premature
  success, bounded waits, sanitized errors) are preserved — the CLI only changes its client
  path, not its authority.
- Telegram and CLI converge on the same pipeline (one turn semantics per transport).

## Rejected alternatives

- **CLI-local TurnService duplicating the pipeline's server-side semantics** — rejected: the
  pipeline's `ContextResolver` binds tasks through the durable DB, which the CLI must not read
  (no local authority). The CLI turn layer is a client-side adapter over the same canonical
  components (`IntentRouter`, `ConversationEngine`, `GatewayClient`) and never re-implements
  binding/DB semantics.
- **Gateway-level `/conversations` + `/turns` REST API now** — rejected for the vertical
  slice: the pipeline already provides the turn semantics; a new server-side API is a
  later wave (P5.4 Wave 3) if needed for multi-client parity.
- **Restoring a pre-durable CLI runtime** — rejected: violates «no second core».
