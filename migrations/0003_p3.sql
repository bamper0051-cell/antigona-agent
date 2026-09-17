BEGIN;

ALTER TABLE task_flows ADD COLUMN parent_id VARCHAR(36) REFERENCES task_flows(id) ON DELETE CASCADE;
ALTER TABLE task_flows ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_flows ADD COLUMN max_depth INTEGER NOT NULL DEFAULT 3;
ALTER TABLE task_flows ADD COLUMN max_child_budget INTEGER NOT NULL DEFAULT 5;

CREATE INDEX ix_task_flows_parent_id ON task_flows (parent_id);

INSERT INTO schema_version(version,applied_at) VALUES(3,CURRENT_TIMESTAMP);
COMMIT;
