-- Migration 0012: delivery read-back columns (B53 / DELIV-02 provider-level
-- confirmation). Additive only. Mirrors the current ORM schema
-- (src/antigona/models.py::DeliveryReceipt) for databases provisioned via the
-- flat migrations/*.sql path instead of Base.metadata.create_all(). The B53
-- read-back fix landed as Alembic revision
-- src/antigona/storage/migrations/versions/0004_delivery_readback.py plus the
-- guarded Python path Database._ensure_delivery_readback_columns(); it bumped
-- SCHEMA_VERSION to 12 without a matching numbered migration in this chain,
-- leaving the flat-SQL <-> ORM parity gap this file closes.
-- All three columns are nullable so pre-B53 rows stay valid without a backfill.
-- Existing SQLite databases are upgraded idempotently by the guarded Python
-- path; this file provisions fresh ones. SQLite has no
-- "ALTER TABLE ... ADD COLUMN IF NOT EXISTS", so this file is not replayable
-- over an already-migrated database (column discovery is required there).

BEGIN;

ALTER TABLE delivery_receipts ADD COLUMN provider_message_id VARCHAR(255);
ALTER TABLE delivery_receipts ADD COLUMN read_back_status VARCHAR(16);
ALTER TABLE delivery_receipts ADD COLUMN read_back_at DATETIME;

INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(12, CURRENT_TIMESTAMP);

COMMIT;
