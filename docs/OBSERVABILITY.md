# P0 observability and session memory

## Mandatory event envelope

Gateway, Worker and Verifier emit one JSON object per log record through
`antigona.observability.event`. Every event requires:

- `timestamp`: RFC 3339 UTC with a `Z` suffix;
- `service`, `event`, and `correlation_id` (the value may be `null` only for process startup or
  database activity outside a flow);
- `task_id`, `session_id`, `step_id`, and `status` on every Gateway, Worker and Verifier event.

The contract is represented by the typed `EventEnvelope` dataclass and enforced at the
`event()` boundary. Identifiers are nullable only when genuinely unknowable: authentication
failure has no authenticated `session_id`; not-found may have only the requested `task_id`;
process startup has no flow identifiers. These exceptions are explicit `null` values, never
missing keys. Database events are a separate operational family: they require service,
correlation and status, but have no flow identifiers.

Success, failure, cancellation, policy denial, authentication denial and verifier rejection
paths use the same envelope. Gateway emits sanitized outcomes for HTTP/WebSocket authentication,
not-found, idempotency conflict, invalid cancellation/approval decisions and validation errors;
only stable reason codes and status values are logged. A complete trajectory is collected by filtering all three service
logs for one exact `correlation_id`. The integration test
`tests/integration/test_observability_trajectory.py` exercises a real successful
Gateway → Worker → Verifier path plus Gateway denial, Worker exception and Verifier rejection.
Durable state changes remain
authoritative; logs are an operational view, not a second state store.

## Secret redaction

Redaction is recursive across mappings and sequences. Secret-key variants include API keys,
tokens, passwords, authorization values, client secrets and credentials, including common
underscore, dash and camel-case spellings. Free-form strings redact complete Bearer values,
inline key/value credentials and the whole HTTP(S) URL authority userinfo. Quoted Bearer values
(including escaped quotes), percent-encoded passwords and adversarial raw `@` in passwords are
covered by exact nested regression tests. Tests use synthetic values only.

## Database instrumentation

Every SQLAlchemy engine created by `Database` installs safe engine hooks for query completion
and failure. Events contain duration, status (`ok`, `slow`, or `error`) and an exception type
for failures. SQL statements and bound parameters are deliberately never logged. The slow
threshold is configured with `Database(..., slow_query_threshold_ms=...)` or
`ANTIGONA_DB_SLOW_QUERY_MS` (default: 250 ms).

## Trust tagging

Workspace reads can mark content as untrusted. That trust state is sticky for the current
agent core: subsequent tool results and artifact evidence retain the untrusted marker, and
shell execution is denied after untrusted content has been read. Worker event records expose
the current trust label for correlation and incident review.

## Conversation summary

The scripted/offline conversation backend stores `<conversation_id>.summary.json` beside its
persistent conversation state. The bounded summary contains only conversation id, turn count,
last tool and last trust label; it intentionally excludes prompt and tool-result bodies.
State and summary files are replaced atomically so a process crash cannot expose a partially
written JSON document.

## Immutable audit trail

`state_transitions` is the durable audit trail. Application code only appends transitions.
Verifier rejection transitions use `TaskRepository.transition`; only the verifier-only
`VERIFYING → DONE` compare-and-set remains an explicit capability path. SQLite deployments
install database triggers that abort every `UPDATE` or `DELETE` against the audit table.
PostgreSQL migration must provide equivalent grants and/or triggers before becoming a
supported durable backend.
