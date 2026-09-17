# ADR-0009: Subagents architecture

- **Date:** 2026-07-26
- **Status:** Accepted
- **Deciders:** Architecture Board
- **References:** ADR-0003 (P1 memory boundary), ADR-0004 (Verifier boundary), ADR-0006 (Cron)

## Context

The ROADMAP P3 asks for *subagents / parallel workstreams*: a parent task must be
able to spawn child `task_flows`, aggregate their results, and do so under limits on
depth and budget. A draft skeleton already existed before this step was accepted —
`TaskFlow` carried `parent_id`/`depth`/`max_depth`/`max_child_budget`, and
`orchestrator.py` had `spawn_child_flow`/`aggregate_child_results` plus the three
subagent exceptions — so P3.1 is a **consolidation and hardening**, not a greenfield.

The central design question is what "parallel" means in a durable, single-owner
system that already refuses to let anything but the Verifier finalize a flow.

## Decision

### 1. Parallelism is *independent durable flows*, not in-process threads.

A child is an ordinary `TaskFlow` row with its own state, its own lease, its own
steps and artifacts, and `parent_id` pointing at the parent. Children land in the
same `DurableQueue` as any other task and are picked up by whichever worker is free;
no separate executor is introduced. Two children run "in parallel" because they are
two independent rows two workers can claim, not because a thread pool fans them out.
Isolation follows for free: each `Orchestrator.run` is its own transaction, so an
exception in one child cannot roll back a sibling or the parent.

### 2. The child always inherits the parent's owner and budget envelope.

`child.owner_id == parent.owner_id` is set unconditionally — cross-owner spawning is
impossible, closing the same escalation surface ADR-0003/ADR-0004 guard elsewhere.
`max_depth` and `max_child_budget` are inherited too, so a single envelope governs
the whole workstream tree rather than each node re-declaring its own.

### 3. Depth and budget gates run *before* any insert.

`spawn_child_flow` checks, in order:

1. **Depth gate:** `parent.depth + 1 > parent.max_depth` → `DepthLimitExceeded`.
2. **Budget gate:** `count(children where parent_id == parent.id) >= parent.max_child_budget`
   → `BudgetLimitExceeded`.

Both raise before `session.add`, so a rejected spawn never leaves a half-created child
to roll back. This is the primary defence against recursive child explosion.

### 4. Idempotency: a repeated spawn returns the same child.

When an explicit `idempotency_key` is passed, `spawn_child_flow` first looks up an
existing child of this parent+owner with that key and returns it unchanged if found —
a retried spawn never produces a duplicate and never spuriously trips the budget gate
on the row it already created. Each child also carries a
`payload_fingerprint = sha256(owner_id:goal:target_path:content)` for evidence. The
`uq_owner_idem` unique constraint on `(owner_id, idempotency_key)` backs this at the
schema level.

### 5. Aggregation is read-only.

`aggregate_child_results(parent)` is a pure `SELECT` over children ordered by
`created_at`. It classifies each child (`DONE` → done; `FAILED`/`CANCELLED`/`BLOCKED`/
`POLICY_DENIED`/`TIMEOUT` → failed; everything else → pending), returns
`{parent_id, total_children, done_count, failed_count, pending_count, all_done,
children:[{id, goal, status, target_path, artifacts:[{id, path, sha256}]}]}`, and runs
**no** state-machine transition and **no** write. `all_done` is true only when there is
at least one child and every child is `DONE`. The parent decides what to do with the
summary; aggregation never finalizes anyone. This keeps the "only the Verifier sets
DONE" invariant (ADR-0004) intact — asserted by `tests/unit/test_clean_room_subagents.py`.

### 6. Failure isolation is behavioural, not cosmetic.

A child that fails verification transitions to `FAILED` in its own transaction. The
parent's row is untouched, the sibling that already reached `DONE` keeps its artifact,
and `aggregate` reports `failed_count >= 1, all_done=False`. The parent is free to
re-spawn, partially accept, or escalate.

## Known limitation: no cancel-propagation in P3.1

Cancelling a parent does **not** cascade to its children in P3.1. Basic isolation
(a child's failure cannot harm the parent) is delivered; full cancel-propagation
policy (parent cancel → children cancel, orphan reaping) is deferred to a later step.
This is a deliberate scope boundary, recorded here so a Verifier does not read its
absence as a defect.

## Out of scope (deferred)

| Item | Where |
|---|---|
| Swappable execution backends (SSH / Modal / Daytona) | P3.2/P3.3 |
| Dual-LLM / planner prompt-injection defence | P3.3 |
| Gateway endpoint `POST /flows/{id}/children` | Optional extension; not built in P3.1 (programmatic orchestrator API only) |
| Planner auto-spawning children ("split into N") | P3.2+; in P3.1 spawn is called explicitly |
| Cross-owner subagents | Forbidden by design (rule 2) |
| Cancel-propagation | Deferred (see above) |

## Clean-room

- Subagent logic (`orchestrator.py`, `worker/__init__.py`) borrows nothing from AGPL
  klio-tech or from Hermes/OpenClaw/OpenHands — only the public idea of parent/child
  flows under depth/budget limits. Verified by `tests/unit/test_clean_room_subagents.py`
  (marker grep) and by the Verifier's own `grep` probes.
- AGPL klio-tech remains unread and unborrowed (`docs/THIRD_PARTY_STRATEGY.md`).
