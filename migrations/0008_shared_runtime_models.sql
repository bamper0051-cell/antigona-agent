-- Migration 0008: shared-Base M1, M2, and RCA runtime tables.
--
-- These models intentionally use antigona.database.Base.  Keep the flat SQL
-- provisioning path in sync with that authoritative metadata just as 0007 does
-- for operations/evidence/Telegram runtime tables.  IF NOT EXISTS makes this
-- safe when an installation already created some tables through create_all().

BEGIN;

CREATE TABLE IF NOT EXISTS kernel_tasks (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(128) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	max_attempts INTEGER NOT NULL,
	attempt_count INTEGER NOT NULL,
	retryable BOOLEAN NOT NULL,
	retry_delay_seconds INTEGER NOT NULL,
	idempotency_key VARCHAR(255) NOT NULL,
	cancel_requested BOOLEAN NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	result JSON,
	error JSON,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_kernel_tasks_owner_id ON kernel_tasks (owner_id);
CREATE INDEX IF NOT EXISTS ix_kernel_tasks_status ON kernel_tasks (status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kernel_tasks_owner_idem
ON kernel_tasks (owner_id, idempotency_key) WHERE idempotency_key != '';

CREATE TABLE IF NOT EXISTS kernel_runs (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	run_number INTEGER NOT NULL,
	attempt INTEGER NOT NULL,
	status VARCHAR(32) NOT NULL,
	lease_owner VARCHAR(128),
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	started_at DATETIME,
	finished_at DATETIME,
	result JSON,
	error JSON,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_kernel_run_task_number UNIQUE (task_id, run_number),
	FOREIGN KEY(task_id) REFERENCES kernel_tasks (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_kernel_runs_status ON kernel_runs (status);
CREATE INDEX IF NOT EXISTS ix_kernel_runs_task_id ON kernel_runs (task_id);

CREATE TABLE IF NOT EXISTS kernel_dependencies (
	id INTEGER NOT NULL,
	child_id VARCHAR(36) NOT NULL,
	parent_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_kernel_dep_child_parent UNIQUE (child_id, parent_id),
	FOREIGN KEY(child_id) REFERENCES kernel_tasks (id) ON DELETE CASCADE,
	FOREIGN KEY(parent_id) REFERENCES kernel_tasks (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_kernel_dependencies_child_id
ON kernel_dependencies (child_id);
CREATE INDEX IF NOT EXISTS ix_kernel_dependencies_parent_id
ON kernel_dependencies (parent_id);

CREATE TABLE IF NOT EXISTS kernel_transitions (
	id INTEGER NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36),
	entity_type VARCHAR(8) NOT NULL,
	entity_id VARCHAR(36) NOT NULL,
	from_state VARCHAR(32),
	to_state VARCHAR(32) NOT NULL,
	reason TEXT NOT NULL,
	actor VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_kernel_transitions_entity_id
ON kernel_transitions (entity_id);
CREATE INDEX IF NOT EXISTS ix_kernel_transitions_run_id
ON kernel_transitions (run_id);
CREATE INDEX IF NOT EXISTS ix_kernel_transitions_task_id
ON kernel_transitions (task_id);

CREATE TABLE IF NOT EXISTS goals (
	id VARCHAR(64) NOT NULL,
	owner_id VARCHAR(128) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	objective TEXT NOT NULL,
	workspace TEXT NOT NULL,
	mutation_required BOOLEAN NOT NULL,
	test_command JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	acceptance_criteria JSON NOT NULL,
	current_flow_id VARCHAR(64),
	cycle_count INTEGER NOT NULL,
	max_cycles INTEGER NOT NULL,
	budget JSON,
	result JSON,
	failure_reason TEXT,
	meta JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_goals_owner_id ON goals (owner_id);
CREATE INDEX IF NOT EXISTS ix_goals_status ON goals (status);

CREATE TABLE IF NOT EXISTS goal_flows (
	id VARCHAR(64) NOT NULL,
	goal_id VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	revision INTEGER NOT NULL,
	"plan" JSON NOT NULL,
	state JSON NOT NULL,
	current_stage VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_goal_flows_goal_id ON goal_flows (goal_id);
CREATE INDEX IF NOT EXISTS ix_goal_flows_status ON goal_flows (status);

CREATE TABLE IF NOT EXISTS wake_events (
	id INTEGER NOT NULL,
	goal_id VARCHAR(64),
	flow_id VARCHAR(64),
	kind VARCHAR(48) NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(16) NOT NULL,
	created_at DATETIME NOT NULL,
	fired_at DATETIME,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_wake_events_goal_id ON wake_events (goal_id);
CREATE INDEX IF NOT EXISTS ix_wake_events_kind ON wake_events (kind);
CREATE INDEX IF NOT EXISTS ix_wake_events_status ON wake_events (status);

CREATE TABLE IF NOT EXISTS service_health (
	service_id VARCHAR(64) NOT NULL,
	state VARCHAR(24) NOT NULL,
	capacity VARCHAR(16) NOT NULL,
	failure_count INTEGER NOT NULL,
	last_failure_class VARCHAR(32),
	last_failure_at DATETIME,
	last_checked_at DATETIME,
	last_probe_ok BOOLEAN,
	observed JSON NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (service_id)
);

CREATE TABLE IF NOT EXISTS service_handoffs (
	id VARCHAR(64) NOT NULL,
	goal_id VARCHAR(64),
	flow_id VARCHAR(64),
	task_id VARCHAR(64),
	original_worker VARCHAR(64) NOT NULL,
	replacement_worker VARCHAR(64) NOT NULL,
	reason_for_handoff TEXT NOT NULL,
	handoff_state JSON NOT NULL,
	created_at DATETIME NOT NULL,
	completed_at DATETIME,
	result JSON,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_service_handoffs_goal_id
ON service_handoffs (goal_id);

CREATE TABLE IF NOT EXISTS rca_error_records (
	rca_id VARCHAR(40) NOT NULL,
	error_id VARCHAR(40) NOT NULL,
	correlation_id VARCHAR(64) NOT NULL,
	source_component VARCHAR(64) NOT NULL,
	category VARCHAR(32) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	confidence VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	duplicate_count INTEGER NOT NULL,
	exception_type VARCHAR(128) NOT NULL,
	error_message TEXT NOT NULL,
	root_cause TEXT NOT NULL,
	user_impact TEXT NOT NULL,
	summary TEXT NOT NULL,
	affected_components TEXT NOT NULL,
	recommended_actions TEXT NOT NULL,
	evidence TEXT NOT NULL,
	suggested_patch TEXT,
	safe_to_auto_fix BOOLEAN NOT NULL,
	requires_owner_approval BOOLEAN NOT NULL,
	hermes_available BOOLEAN NOT NULL,
	tool_name VARCHAR(128),
	provider VARCHAR(64),
	model VARCHAR(128),
	task_id VARCHAR(40),
	flow_id VARCHAR(40),
	step_id VARCHAR(40),
	session_id VARCHAR(40),
	git_revision VARCHAR(64) NOT NULL,
	runtime_metadata JSON NOT NULL,
	diagnostic_hints TEXT NOT NULL,
	analysis_duration_ms FLOAT NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (rca_id)
);
CREATE INDEX IF NOT EXISTS ix_rca_error_records_correlation_id
ON rca_error_records (correlation_id);
CREATE INDEX IF NOT EXISTS ix_rca_error_records_error_id
ON rca_error_records (error_id);
CREATE INDEX IF NOT EXISTS ix_rca_error_records_fingerprint
ON rca_error_records (fingerprint);
CREATE INDEX IF NOT EXISTS ix_rca_error_records_source_component
ON rca_error_records (source_component);
CREATE INDEX IF NOT EXISTS ix_rca_error_records_task_id
ON rca_error_records (task_id);

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES(8, CURRENT_TIMESTAMP);

COMMIT;
