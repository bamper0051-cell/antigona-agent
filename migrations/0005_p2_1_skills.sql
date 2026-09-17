BEGIN;

ALTER TABLE skills ADD COLUMN owner_id VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE skills ADD COLUMN slug VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE skills ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE skills ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'DRAFT';
ALTER TABLE skills ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE skills ADD COLUMN format_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE skills ADD COLUMN body_sha256 VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE skills ADD COLUMN body_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE skills ADD COLUMN body_path TEXT NOT NULL DEFAULT '';
ALTER TABLE skills ADD COLUMN trust VARCHAR(16) NOT NULL DEFAULT 'trusted';
ALTER TABLE skills ADD COLUMN risk_ceiling VARCHAR(16) NOT NULL DEFAULT 'MEDIUM';
ALTER TABLE skills ADD COLUMN source_flow_id VARCHAR(36);
ALTER TABLE skills ADD COLUMN verified_at DATETIME;
ALTER TABLE skills ADD COLUMN verified_by VARCHAR(128);
-- SQLite refuses a non-constant default in ADD COLUMN once the table holds rows, so the
-- placeholder below is immediately replaced by the row's own created_at.
ALTER TABLE skills ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00';
UPDATE skills SET updated_at = created_at;

-- Deterministic backfill of legacy rows: slug derived from the old name, everything
-- else from the column defaults (status = DRAFT).
UPDATE skills
   SET slug = trim(
         substr(
           lower(replace(replace(replace(replace(name, ' ', '-'), '_', '-'), '.', '-'), '/', '-')),
           1, 64),
         '-');

-- SQLite has no regex, so anything the ASKILL/1 slug grammar rejects (any character
-- outside a-z0-9- survived the substitutions, or nothing survived at all) falls back to
-- the row id — the same shape skill_slug() falls back to in src/antigona/models.py.
UPDATE skills
   SET slug = substr('skill-' || replace(lower(id), '-', ''), 1, 64)
 WHERE slug = '' OR slug GLOB '*[^a-z0-9-]*';

-- Legacy rows that collapse onto the same (owner_id, slug, version) keep one winner;
-- the rest are disambiguated by their immutable id before the unique index is built.
UPDATE skills
   SET slug = substr(slug || '-' || replace(lower(id), '-', ''), 1, 64)
 WHERE rowid NOT IN (SELECT min(rowid) FROM skills GROUP BY owner_id, slug, version);

CREATE INDEX ix_skills_owner_id ON skills (owner_id);
CREATE INDEX ix_skills_slug ON skills (slug);
CREATE UNIQUE INDEX uq_skill_owner_slug_version ON skills (owner_id, slug, version);

CREATE TABLE skill_transitions (
	id VARCHAR(36) NOT NULL,
	skill_id VARCHAR(36) NOT NULL,
	from_status VARCHAR(16) NOT NULL,
	to_status VARCHAR(16) NOT NULL,
	actor VARCHAR(64) NOT NULL,
	reason TEXT,
	correlation_id VARCHAR(36),
	accepted BOOLEAN NOT NULL DEFAULT 0,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_skill_transitions_skill_id ON skill_transitions (skill_id);

CREATE TRIGGER IF NOT EXISTS skill_transitions_no_update
BEFORE UPDATE ON skill_transitions BEGIN
  SELECT RAISE(ABORT, 'skill_transitions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS skill_transitions_no_delete
BEFORE DELETE ON skill_transitions BEGIN
  SELECT RAISE(ABORT, 'skill_transitions is append-only');
END;

INSERT INTO schema_version(version,applied_at) VALUES(5,CURRENT_TIMESTAMP);
COMMIT;
