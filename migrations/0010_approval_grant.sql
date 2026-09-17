-- Migration 0010: durable one-shot approval grants for owner decisions (A-1).
--
-- An owner approval used to be a bare string ("APPROVED") in `approvals`,
-- which authorized an unbounded number of executions. The decision now mints
-- a one-shot ApprovalGrantStore grant; the raw token lives in its own column
-- (never in `arguments`, which the gateway serializes into API responses) and
-- is consumed exactly once by the executing path.
--
-- Existing SQLite databases are upgraded idempotently by
-- Database.create_all()::_ensure_approval_grant_column.

ALTER TABLE approvals ADD COLUMN grant_token VARCHAR(128);

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES(10, CURRENT_TIMESTAMP);
