# ADR-0011: Dual-LLM quarantine model for untrusted content

- **Date:** 2026-07-26
- **Status:** Accepted

## Context

In `WorkerAgentCore`, untrusted inputs such as web fetch results and files marked with `untrusted=True` trigger trust degradation (`self._degrade_trust()`). While `untrusted_context` disables dangerous operations like the shell tool (`sandbox.shell`), trust degradation is a post-ingestion backstop. 

Prior to P3.3, raw untrusted text (including prompt injection attempts) was passed directly into the main tool-using agent's reasoning context. A malicious payload embedded in fetched web pages or files could influence agent planning before trust degradation took effect.

To close this gap without crippling the main agent's ability to solve open-ended tasks (which occurs under full capability-based CaMeL models), P3.3 introduces a pragmatic **Dual-LLM quarantine model**.

## Decision

1. **Tool-less Quarantine Model:** 
   - Untrusted content is routed through an isolated, tool-less LLM instance (`QuarantineModel`).
   - The quarantine model has no access to tools or execution capabilities. Its sole function is to sanitize untrusted input and extract safe facts (`QuarantineResult.safe_facts`).
2. **Model Collision Guard:**
   - `quarantine_model` must differ from `model_primary`. If `quarantine_model == model_primary` (when `quarantine_model != "none"`), initialization raises `ModelCollisionError` (mirroring `LLMJudge` verifier collision guard).
3. **Dual Mock / Real Provider Architecture:**
   - `MockQuarantineProvider`: Deterministic in-memory provider that strips known prompt injection markers without network or SDK dependencies (used in CI/tests and `quarantine_model="none"` mode).
   - `HTTPQuarantineProvider`: OpenAI-compatible transport with lazy `urllib` import that issues tool-less prompts to external models. Real transport tests run only when `ANTIGONA_OPENROUTER_API_KEY` is present.
4. **Fail-Closed Strategy on Transport Failure:**
   - If the quarantine provider encounters a transport error (`ProviderTransportError` or `ProviderMalformedResponse`), `QuarantineModel.sanitize()` raises `QuarantineUnavailableError`.
   - `WorkerAgentCore` catches `QuarantineUnavailableError`, triggers `_degrade_trust()`, and returns `[QUARANTINE_UNAVAILABLE]` instead of raw text. Raw untrusted bytes NEVER reach the main agent's reasoning context.
5. **Backstop Trust Degradation:**
   - `_degrade_trust()` remains active as an invariant backstop whenever untrusted inputs are fetched or read, or when injection/degradation is detected.
6. **Verifier Invariant Intact:**
   - Only the independent Verifier sets `DONE`. Neither `QuarantineModel` nor `WorkerAgentCore` alters flow state transitions.

## Consequences

- The primary tool-using agent never ingests raw untrusted payload into its reasoning context.
- Open-ended tool use and capabilities of the main agent are fully preserved.
- Fail-closed behavior guarantees safety even during network degradation or provider downtime.
- Clean-room constraints are satisfied: no code or patterns are borrowed from AGPL klio-tech / Hermes / OpenClaw.
