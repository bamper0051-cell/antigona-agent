# ADR-0015: Multi-channel Delivery Adapters (Discord, Slack, WhatsApp, Signal, Email)

- **Date:** 2026-07-26
- **Status:** Accepted
- **Full text:** `docs/adr/0015-delivery-adapters.md`

## Context

Prior to P5.1, Antigona provided an outbox table (`delivery_outbox`) and a single send-only adapter (`TelegramAdapter`).
The worker (`delivery_worker.py`) instantiated a single hardcoded Telegram adapter without inspecting the `DeliveryOutbox.adapter` column.
To support multi-channel notification and event delivery across diverse operations (Discord, Slack, WhatsApp, Signal, Email), Antigona requires a multi-channel delivery architecture built on top of the existing outbox state engine.

## Decision

1. **Unified Interface (`DeliveryAdapter`):**
   All channels implement a single `DeliveryAdapter` protocol with `name: str` and `deliver(event: ProgressEvent, idempotency_key: str) -> None`.
   Adapters do not implement internal state transitions or custom outbox status updates.

2. **Channel Factory (`get_adapter`) & Router (`Router`):**
   `get_adapter(channel_name, settings)` maps a requested channel string to its adapter instance.
   An unknown channel raises an explicit `UnknownChannelError` — no silent fallback to Telegram or progress.
   `adapter="progress"` maps to `settings.delivery_default_channel` (default: `"telegram"`) for backward compatibility.
   `Router` caches adapter instances per worker process and delegates delivery calls.

3. **Fail-Closed & Fail-Safe Delivery:**
   When an external API (Discord, Slack, WhatsApp, Signal, Email) is unavailable or encounters an error, the adapter raises an exception.
   `DeliveryWorker` catches the exception, retains the message in `DeliveryOutbox` with status `PENDING`, records `last_error`, and applies exponential backoff (`available_at`).
   No message is dropped or silently marked `DELIVERED`.
   Only the Verifier places tasks into `DONE` state — delivery adapters never alter task execution status.

4. **Dual Mock / Real Adapter Architecture with Lazy SDK Imports:**
   All 5 new channel adapters (`discord`, `slack`, `whatsapp`, `signal`, `email`) support:
   - **Mock runtime:** Active when `settings.delivery_mock=True` or when credentials are not configured. Delivery events are logged / recorded in-memory without opening network sockets or calling SDKs.
   - **Real runtime:** Lazy SDK / module import performed inside `deliver()` or `connect()`, preventing unused client dependencies from slowing initialization or breaking offline CI environments.

5. **Strict Clean-Room Boundary:**
   No code, structures, or identifiers are borrowed from AGPL klio-tech, Hermes, or OpenClaw implementations.

## Consequences

- Backward compatibility for existing outbox records (`adapter="progress"`) and `TelegramAdapter` remains completely intact.
- Multi-channel delivery capability is available seamlessly across CLI, worker processes, and integration pipelines.
- CI environments run 100% offline with zero external network dependency.
