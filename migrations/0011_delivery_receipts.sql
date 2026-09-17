-- Migration 0011: delivery_receipts (DELIV-01 / P2 durable crash-after-send idempotency).
-- Additive only. Mirrors the current ORM schema (src/antigona/models.py::DeliveryReceipt)
-- for databases provisioned via the flat migrations/*.sql path instead of
-- Base.metadata.create_all(). The DeliveryReceipt model was added with the DELIV-01 fix
-- without a matching numbered migration; this closes that flat-SQL <-> ORM parity gap.
-- Existing SQLite databases already have the table via create_all(); IF NOT EXISTS keeps
-- this replay-safe.

BEGIN;

CREATE TABLE IF NOT EXISTS delivery_receipts (
	idempotency_key VARCHAR(128) NOT NULL,
	adapter VARCHAR(32) NOT NULL,
	task_id VARCHAR(36),
	delivered_at DATETIME NOT NULL,
	transmitted BOOLEAN NOT NULL,
	PRIMARY KEY (idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_delivery_receipts_task_id ON delivery_receipts (task_id);

INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(11, CURRENT_TIMESTAMP);

COMMIT;
