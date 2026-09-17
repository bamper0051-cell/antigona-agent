-- Migration 0006: P2.2 cron scheduler tables
-- Adds schedule_events append-only journal table
-- CronSchedule table already created by ORM create_all()

CREATE TABLE IF NOT EXISTS schedule_events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    schedule_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(16) NOT NULL,
    message TEXT,
    correlation_id VARCHAR(36) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_schedule_events_schedule_id ON schedule_events (schedule_id);
