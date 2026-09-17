CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id UUID PRIMARY KEY,
    owner_id VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('session', 'profile')),
    profile_key VARCHAR(128),
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    source_ref VARCHAR(255),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT profile_key_matches_kind CHECK (
        (kind = 'profile' AND profile_key IS NOT NULL)
        OR (kind = 'session' AND profile_key IS NULL)
    ),
    CONSTRAINT uq_profile_memory UNIQUE (owner_id, profile_key)
);

CREATE INDEX IF NOT EXISTS ix_memory_entries_owner_kind
    ON memory_entries (owner_id, kind);
CREATE INDEX IF NOT EXISTS ix_memory_entries_embedding_hnsw
    ON memory_entries USING hnsw (embedding vector_cosine_ops);

INSERT INTO memory_schema_version (version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
