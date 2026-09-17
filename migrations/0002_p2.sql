BEGIN;

CREATE TABLE skills (
	id VARCHAR(36) NOT NULL,
	name VARCHAR(128) NOT NULL,
	trigger TEXT NOT NULL,
	trajectory_ref VARCHAR(128) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_skills_name ON skills (name);
CREATE INDEX ix_skills_trajectory_ref ON skills (trajectory_ref);

CREATE TABLE cron_schedules (
	id VARCHAR(36) NOT NULL,
	name VARCHAR(128) NOT NULL,
	cron_expression VARCHAR(64) NOT NULL,
	owner_id VARCHAR(128) NOT NULL,
	goal TEXT NOT NULL,
	target_path TEXT NOT NULL,
	content TEXT NOT NULL,
	tool_name VARCHAR(64) NOT NULL,
	tool_arguments JSON NOT NULL,
	enabled BOOLEAN NOT NULL,
	cancelled BOOLEAN NOT NULL,
	last_run_at DATETIME,
	next_run_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_cron_schedules_name ON cron_schedules (name);
CREATE INDEX ix_cron_schedules_owner_id ON cron_schedules (owner_id);
CREATE INDEX ix_cron_schedules_next_run_at ON cron_schedules (next_run_at);

INSERT INTO schema_version(version,applied_at) VALUES(2,CURRENT_TIMESTAMP);
COMMIT;
