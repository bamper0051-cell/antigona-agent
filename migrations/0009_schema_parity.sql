-- Migration 0009: canonical flat/ORM schema parity.
--
-- Fresh flat provisioning is canonical in 0001/0002/0004/0006/0007 so each
-- table is born with the authoritative ORM shape.  Existing SQLite databases
-- are upgraded idempotently by Database.create_all()::_ensure_schema_v9;
-- column discovery is required because SQLite has no ADD COLUMN IF NOT EXISTS.
-- This replay-safe marker keeps the flat chain version unambiguous.

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES(9, CURRENT_TIMESTAMP);
