# ADR 0004: P1.2 independent verifier boundary

- Status: accepted
- Date: 2026-07-25

## Decision

Verifier v2 uses an explicitly injected `VerifierProvider`. Every provider verdict carries the
provider-reported `actual_model`; a configured/reported model mismatch fails closed. The primary
and verifier model identifiers must differ.

Completion criteria are persisted in `verifier_criteria`, whose mapped class, declarative metadata,
and session factory live only in `antigona.verifier.criteria`. The shared `TaskFlow` model has no
relationship, property, backref, mapped table, or criteria symbol; worker and gateway therefore have
no shared-ORM query path to the hidden value. The task identifier remains opaque to the private
store, while migration `0004` preserves the deployed database-level foreign key and is idempotent.
Only the verifier service initializes and queries the private store. A missing private criterion,
provider transport/schema failure, negative verdict, trajectory anomaly, or artifact hash mismatch
prevents `DONE` and records a state-machine transition to `FAILED`.

Artifact read-back is fail-closed at the opened-file-descriptor boundary. Every path component is
opened without following symlinks, and a regular artifact is accepted only when `st_nlink == 1`
both before and after reading. Hardlinks are therefore rejected even when their other name is inside
the workspace or has identical content; this prevents workspace names from laundering an inode from
outside the trust boundary. Device, inode, size, and link count must remain stable during the read,
then the namespace is walked again and matched to the opened inode.

Production has no deterministic fallback. Hermetic tests inject a deterministic provider that
implements the same `ProviderResult` contract and seed criteria through `VerifierCriteriaStore`.

Trajectory continuity is evaluated only across task transitions; step transitions share the
journal but are a separate state machine and must not create false discontinuity findings.

## Verification

- P1.2 targeted: 10 passed.
- Migrated P0/recovery/HITL/integration harnesses: 35 passed.
- Full suite without pytest cache: 141 passed, 3 skipped.
- Ruff, mypy (`src`), compileall, and `git diff --check`: clean.
