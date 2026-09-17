-- Migration 0007: runtime tables (operations, evidence_registry, telegram_message_bindings)
-- Additive only. Mirrors the current ORM schema (src/antigona/durable/operation_models.py,
-- src/antigona/models.py) for databases provisioned via the flat migrations/*.sql path
-- instead of Base.metadata.create_all().

BEGIN;

CREATE TABLE IF NOT EXISTS operations (
	id VARCHAR(36) NOT NULL,
	chat_id BIGINT NOT NULL,
	user_id BIGINT,
	origin_message_id INTEGER NOT NULL,
	progress_message_id INTEGER,
	reply_to_message_id INTEGER,
	intent TEXT,
	flow_id TEXT,
	tool_call_id TEXT,
	status TEXT NOT NULL,
	current_stage TEXT,
	current_step INTEGER NOT NULL,
	total_steps INTEGER NOT NULL,
	text TEXT,
	final_message_ids JSON NOT NULL,
	executed_tools JSON NOT NULL DEFAULT '{}',
	last_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_operations_status ON operations (status);
CREATE INDEX IF NOT EXISTS ix_operations_chat_id ON operations (chat_id);
CREATE INDEX IF NOT EXISTS ix_operations_chat_status ON operations (chat_id, status);

CREATE TABLE IF NOT EXISTS evidence_registry (
	evidence_id VARCHAR(128) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	attempt_id VARCHAR(128) NOT NULL,
	type VARCHAR(64) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	outcome VARCHAR(32) NOT NULL,
	correlation_id VARCHAR(64) NOT NULL,
	source VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	sha256 VARCHAR(64),
	artifact_reference TEXT,
	status VARCHAR(16) NOT NULL,
	supports_claims JSON NOT NULL,
	verified_by VARCHAR(128),
	PRIMARY KEY (evidence_id)
);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_task_id ON evidence_registry (task_id);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_attempt_id ON evidence_registry (attempt_id);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_status ON evidence_registry (status);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_outcome ON evidence_registry (outcome);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_correlation_id ON evidence_registry (correlation_id);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_source ON evidence_registry (source);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_created_at ON evidence_registry (created_at);
CREATE INDEX IF NOT EXISTS ix_evidence_registry_kind ON evidence_registry (kind);

CREATE TABLE IF NOT EXISTS telegram_message_bindings (
	id VARCHAR(36) NOT NULL,
	chat_id BIGINT NOT NULL,
	telegram_message_id INTEGER NOT NULL,
	user_id BIGINT,
	task_id VARCHAR(36),
	session_id VARCHAR(36),
	step_id VARCHAR(36),
	correlation_id VARCHAR(36),
	message_role VARCHAR(16) NOT NULL,
	message_kind VARCHAR(32) NOT NULL,
	source_message_id INTEGER,
	original_text TEXT,
	edited_text TEXT,
	edit_version INTEGER NOT NULL,
	metadata_json JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_telegram_msg_binding UNIQUE (chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS ix_telegram_message_bindings_task_id ON telegram_message_bindings (task_id);
CREATE INDEX IF NOT EXISTS ix_telegram_message_bindings_chat_id ON telegram_message_bindings (chat_id);

INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(7, CURRENT_TIMESTAMP);
COMMIT;

-- Единая память (Step 5-6 манифеста): таблица memory_entries в основной БД.
CREATE TABLE IF NOT EXISTS memory_entries (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(128) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	title VARCHAR(255) NOT NULL,
	content TEXT NOT NULL,
	source VARCHAR(32) NOT NULL,
	task_id VARCHAR(36),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_memory_entries_owner_id ON memory_entries (owner_id);
CREATE INDEX IF NOT EXISTS ix_memory_entries_kind ON memory_entries (kind);
CREATE INDEX IF NOT EXISTS ix_memory_entries_task_id ON memory_entries (task_id);
