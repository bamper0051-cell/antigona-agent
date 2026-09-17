# DECISION LEDGER (L4 — Immutable Owner Decisions)

Append-only. Entries are never modified; superseding entries reference prior
IDs. Format per architecture §8.3 L4.

---

## D-001 — P0_ACCEPTED (Execution Truthfulness)
- **date**: 2026-07-31
- **owner**: Owner
- **subject**: P0 Execution Truthfulness acceptance
- **selected_decision**: `P0_ACCEPTED`
- **reason**: routing defect fixed (F.text last); control-plane auth commands
  bypass conversational LLM; routing suite 8/8; live runtime verified; single
  polling instance; no unverified execution claims; invariant upheld
  (NO VERIFIED EXECUTION RESULT → NO EXECUTION CLAIM); 3 independent reviews
  without unresolved criticals; zero deterministic P0 regressions.
- **evidence**: docs/p0/ (baseline, tests, reviews, OWNER_REVIEW_PACKAGE,
  LIVE_E2E_EVIDENCE, PACKAGE_SHA256)
- **supersedes**: (none)
- **affected_components**: Telegram routing, auth command namespace, presenter
  truthfulness contract
- **notes**: e2e_live2.py import-time incident recorded as minor → TASK-P0-DEBT-03.
  Debt registered: TASK-P0-DEBT-01..04 (docs/ROADMAP.md).

---

## D-002 — START P1 (Owner Authentication)
- **date**: 2026-07-31
- **owner**: Owner
- **subject**: Authorization to start P1 (Phase 7)
- **selected_decision**: `START P1`
- **reason**: P0 accepted; scope strictly Phase 7; execution pipeline / Hermes
  Core / routing / Gateway / Worker / LLM untouched; parallel work limited to
  TASK-P0-DEBT-03 (import-safe E2E tooling).
- **evidence**: docs/ROADMAP.md, docs/ROADMAP.md
- **supersedes**: (none)
- **affected_components**: auth subsystem only

---

## D-003 — LIVE DEPLOY + SMOKE AUTHORIZED (P1)
- **date**: 2026-07-31
- **owner**: Owner
- **subject**: Controlled bot restart with P1 code + live smoke (Step 7.8)
- **selected_decision**: `LIVE DEPLOY: AUTHORIZED / LIVE SMOKE: AUTHORIZED`
- **reason**: close OPS-P1-1; precondition: precise rollback incl. bot.py;
  smoke scenario 3.1–3.7; evidence correlation per step; manifest rebuild rule
  (self-exclusion); P1 acceptance remains pending.
- **evidence**: docs/p1/rollback/ (4 P0 files + script), docs/p1/evidence/
  LIVE_SMOKE_BLOCKER.md, LIVE_SMOKE_EVIDENCE.md, RESTART_SEMANTICS_EVIDENCE.md
- **supersedes**: (none)
- **affected_components**: live bot runtime

---

## D-004 — P1_ACCEPTED (Owner Authentication)
- **date**: 2026-07-31
- **owner**: Owner
- **subject**: P1 acceptance
- **selected_decision**: `P1_ACCEPTED`
- **reason**: P1 code live; live smoke 6/6; fail-closed owner/auth paths;
  wrong PIN → no session; correct PIN → session TTL 900s; /confirm INVALID
  rejected without execution; /pin owner-gated; transient restart semantics
  live-verified; no LLM/Gateway/Worker calls from auth commands; no PIN/token
  in logs or evidence; single polling instance; P0 routing invariant GREEN;
  critical/major findings closed or refuted; rollback prepared for all 4
  runtime files incl. bot.py; evidence manifest SHA256-verified.
- **evidence**: docs/ROADMAP.md, docs/p1/evidence/,
  docs/p1/reviews/ (ARCH/SEC/OPS/CONFLICT_MATRIX), docs/ROADMAP.md
- **supersedes**: (none)
- **affected_components**: auth subsystem (live)
- **notes**: documentation cleanup mandated before P2: OWNER_REVIEW_PACKAGE
  runtime statement refreshed; rollback now covers bot.py everywhere; R1
  CLOSED; SEC_REVIEW disposition added; this ledger entry. P2 AUTHORIZED TO
  START after cleanup; P3+ LOCKED until separate Owner decision.

---

## D-005 — P2_ACCEPTED (Hermes Deterministic Core)
- **date**: 2026-07-31
- **owner**: Owner
- **subject**: P2 acceptance
- **selected_decision**: `P2_ACCEPTED`
- **scope**: Hermes Deterministic Core (Task Registry, State Machine, Event
  Log, Provider Registry, Evidence Registry, Owner Gate integration)
- **basis**: Round 3 transition-specific evidence policy (single
  EXECUTION_CLAIMING_STATES source of truth; guards before CAS + append;
  DONE requires strictly typed EXECUTION_RESULT with VERIFIED + SUCCESS +
  execution-authoritative source + task_id/correlation_id match + verifier
  capability; FAILED/UNAVAILABLE/PARTIAL and non-authoritative sources and
  approval evidence cannot resolve DONE; fail-closed foreign task/correlation);
  106 passing tests; ruff/mypy/py_compile clean; self-contained verified
  Evidence Package (57-file manifest ALL OK, archive hash verified);
  P0 routing invariant GREEN; P1 Owner Auth GREEN; no unresolved
  critical/major.
- **invariant**: `NO VERIFIED SUCCESSFUL EXECUTION RESULT → NO EXECUTION CLAIM`
- **supersedes**: (none — follows D-004)
- **affected_components**: src/antigona/core/* (new), durable/state_machine.py
  (additive), models.py (EvidenceRecord +kind/outcome/correlation_id)
- **notes**: residual risks requiring direct DB access or future runtime
  wiring accepted as P2 residuals (see RISK_REPORT P2-R11), to be revisited
  at Gateway/Worker integration. Acceptance does NOT permit bypassing
  Registry/Event Log/Evidence Policy/Owner Gate during later wiring.
  P3 AUTHORIZED TO PLAN only; P3 implementation requires a separate Start
  Decision (scope, dependencies, migration boundaries, acceptance criteria).
  Production runtime changes and P2 wiring into execution pipeline NOT
  allowed until then.
