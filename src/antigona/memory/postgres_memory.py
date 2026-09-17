from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

EMBEDDING_DIMENSIONS = 1536


class MemoryKind(StrEnum):
    SESSION = "session"
    PROFILE = "profile"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    owner_id: str
    kind: MemoryKind
    content: str
    profile_key: str | None
    source_ref: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    similarity: float = 1.0


class PostgresMemoryStore:
    """Durable, owner-isolated vector and profile memory backed by pgvector."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("memory store requires a PostgreSQL URL")
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        connection = psycopg.connect(self.database_url, row_factory=dict_row)
        register_vector(connection)
        return connection

    def migrate(self) -> None:
        migration = (
            files("antigona").joinpath("sql", "memory_v1.sql").read_text(encoding="utf-8")
        )
        with psycopg.connect(self.database_url) as connection:
            # Serialize schema setup across concurrently starting processes. PostgreSQL DDL
            # is transactional, so a failed migration cannot leave a half-created schema.
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (0x414E5449474F4E41,))
            connection.execute(migration)

    def remember(
        self,
        *,
        owner_id: str,
        content: str,
        embedding: list[float],
        kind: MemoryKind = MemoryKind.SESSION,
        source_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        if kind is MemoryKind.PROFILE:
            raise ValueError("profile memories require upsert_profile and a stable key")
        vector = self._validate_embedding(embedding)
        memory_id = str(uuid.uuid4())
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO memory_entries
                   (id, owner_id, kind, content, embedding, source_ref, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, owner_id, kind, content, profile_key, source_ref,
                             metadata, created_at, updated_at""",
                (
                    memory_id,
                    owner_id,
                    kind.value,
                    content,
                    vector,
                    source_ref,
                    Jsonb(metadata or {}),
                ),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def upsert_profile(
        self,
        *,
        owner_id: str,
        key: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        vector = self._validate_embedding(embedding)
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO memory_entries
                   (id, owner_id, kind, profile_key, content, embedding, metadata)
                   VALUES (%s, %s, 'profile', %s, %s, %s, %s)
                   ON CONFLICT (owner_id, profile_key) DO UPDATE SET
                     content = EXCLUDED.content,
                     embedding = EXCLUDED.embedding,
                     metadata = EXCLUDED.metadata,
                     updated_at = CURRENT_TIMESTAMP
                   RETURNING id, owner_id, kind, content, profile_key, source_ref,
                             metadata, created_at, updated_at""",
                (str(uuid.uuid4()), owner_id, key, content, vector, Jsonb(metadata or {})),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def search(
        self,
        *,
        owner_id: str,
        embedding: list[float],
        limit: int = 5,
        kind: MemoryKind | None = None,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        vector = self._validate_embedding(embedding)
        kind_filter = "AND kind = %s" if kind is not None else ""
        parameters: list[Any] = [vector, owner_id]
        if kind is not None:
            parameters.append(kind.value)
        parameters.extend([vector, limit])
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT id, owner_id, kind, content, profile_key, source_ref,
                           metadata, created_at, updated_at,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM memory_entries
                    WHERE owner_id = %s {kind_filter}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s""",
                parameters,
            ).fetchall()
        return [self._record(row) for row in rows]

    def delete_owner(self, owner_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_entries WHERE owner_id = %s", (owner_id,)
            )
            return cursor.rowcount

    @staticmethod
    def _validate_embedding(embedding: list[float]) -> list[float]:
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"embedding must contain exactly {EMBEDDING_DIMENSIONS} values")
        vector = [float(value) for value in embedding]
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("embedding values must be finite")
        return vector

    @staticmethod
    def _record(row: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            owner_id=str(row["owner_id"]),
            kind=MemoryKind(row["kind"]),
            content=str(row["content"]),
            profile_key=row["profile_key"],
            source_ref=row["source_ref"],
            metadata=dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            similarity=float(row.get("similarity", 1.0)),
        )
