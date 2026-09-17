PRAGMA foreign_keys=ON;
BEGIN;
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE task_flows (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(128) NOT NULL,
	goal TEXT NOT NULL,
	target_path TEXT NOT NULL,
	content TEXT NOT NULL,
	payload_fingerprint VARCHAR(64) NOT NULL,
	tool_name VARCHAR(64) NOT NULL,
	tool_arguments JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	revision INTEGER NOT NULL,
	cancellation_requested BOOLEAN NOT NULL,
	idempotency_key VARCHAR(255) NOT NULL,
	checkpoint VARCHAR(64) NOT NULL,
	side_effect_key VARCHAR(64),
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_owner_idem UNIQUE (owner_id, idempotency_key)
)

;
CREATE INDEX ix_task_flows_owner_id ON task_flows (owner_id);

CREATE TABLE approvals (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	tool_name VARCHAR(128) NOT NULL,
	arguments JSON NOT NULL,
	risk_level VARCHAR(16) NOT NULL,
	reason TEXT NOT NULL,
	decision VARCHAR(16) NOT NULL,
	decided_by VARCHAR(128),
	decided_at DATETIME,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id)
)

;
CREATE INDEX ix_approvals_task_id ON approvals (task_id);

CREATE TABLE delivery_outbox (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	adapter VARCHAR(32) NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	payload JSON NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at DATETIME NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	delivered_at DATETIME,
	last_error TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id),
	UNIQUE (idempotency_key)
)

;
CREATE INDEX ix_delivery_outbox_task_id ON delivery_outbox (task_id);
CREATE INDEX ix_delivery_outbox_status ON delivery_outbox (status);

CREATE TABLE flow_steps (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	"index" INTEGER NOT NULL,
	title VARCHAR(255) NOT NULL,
	step_number INTEGER NOT NULL DEFAULT 0,
	tool_name VARCHAR(128) NOT NULL DEFAULT '',
	arguments JSON NOT NULL DEFAULT '{}',
	status VARCHAR(32) NOT NULL,
	revision INTEGER NOT NULL,
	input JSON NOT NULL,
	output JSON,
	retries INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id)
)

;
CREATE INDEX ix_flow_steps_task_id ON flow_steps (task_id);

CREATE TABLE queue_jobs (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	lane VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at DATETIME NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	last_error TEXT,
	correlation_id VARCHAR(36) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id)
)

;
CREATE INDEX ix_queue_jobs_status ON queue_jobs (status);
CREATE UNIQUE INDEX ix_queue_jobs_task_id ON queue_jobs (task_id);

CREATE TABLE state_transitions (
	id INTEGER NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	entity_id VARCHAR(36) NOT NULL,
	entity_type VARCHAR(16) NOT NULL,
	from_state VARCHAR(32),
	to_state VARCHAR(32) NOT NULL,
	reason TEXT NOT NULL,
	actor VARCHAR(64) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id)
)

;
CREATE INDEX ix_state_transitions_task_id ON state_transitions (task_id);

CREATE TABLE artifacts (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	step_id VARCHAR(36) NOT NULL,
	path TEXT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	size INTEGER NOT NULL,
	verified BOOLEAN NOT NULL,
	evidence JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id),
	FOREIGN KEY(step_id) REFERENCES flow_steps (id)
)

;
CREATE INDEX ix_artifacts_task_id ON artifacts (task_id);

CREATE TABLE durable_operations (
	id VARCHAR(64) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	step_id VARCHAR(36) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	request JSON NOT NULL,
	result JSON,
	attempts INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES task_flows (id),
	FOREIGN KEY(step_id) REFERENCES flow_steps (id)
)

;
CREATE INDEX ix_durable_operations_task_id ON durable_operations (task_id);
INSERT INTO schema_version(version,applied_at) VALUES(1,CURRENT_TIMESTAMP);
COMMIT;
