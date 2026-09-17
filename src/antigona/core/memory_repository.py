"""Единый репозиторий памяти Antigona (Step 5-6 манифеста).

Одна система памяти в основной БД (таблица ``memory_entries``).
Только ядро (Gateway) модифицирует память; CLI и Telegram работают
через API (``GET/POST /api/v1/memory``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..database import Database
from ..models import MemoryEntry

VALID_KINDS = frozenset({"fact", "preference", "profile", "user", "memory"})
VALID_SOURCES = frozenset({"user", "task", "core"})


class MemoryRepository:
    """Sync-репозиторий поверх основной БД (таблица memory_entries)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ── Write (только ядро) ─────────────────────────────────────────────

    def remember(
        self,
        owner_id: str,
        content: str,
        *,
        kind: str = "fact",
        title: str = "",
        source: str = "user",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Сохранить факт/предпочтение/профиль в единую память."""
        if kind not in VALID_KINDS:
            kind = "fact"
        if source not in VALID_SOURCES:
            source = "user"
        if not title:
            title = content.strip().splitlines()[0][:255] if content.strip() else "untitled"
        with self._database.session_factory() as session:
            entry = MemoryEntry(
                owner_id=owner_id,
                kind=kind,
                title=title,
                content=content.strip(),
                source=source,
                task_id=task_id,
            )
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return self._to_dict(entry)

    def forget(self, owner_id: str, entry_id: str) -> bool:
        """Удалить запись памяти (только своей owner_id)."""
        with self._database.session_factory() as session:
            entry = session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.id == entry_id,
                    MemoryEntry.owner_id == owner_id,
                )
            )
            if entry is None:
                return False
            session.delete(entry)
            session.commit()
            return True

    def clear(self, owner_id: str) -> int:
        """Очистить всю память владельца. Возвращает число удалённых записей."""
        with self._database.session_factory() as session:
            entries = list(
                session.scalars(
                    select(MemoryEntry).where(MemoryEntry.owner_id == owner_id)
                )
            )
            for entry in entries:
                session.delete(entry)
            session.commit()
            return len(entries)

    # ── Read ─────────────────────────────────────────────────────────────

    def list_entries(
        self,
        owner_id: str,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._database.session_factory() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.owner_id == owner_id)
            if kind:
                stmt = stmt.where(MemoryEntry.kind == kind)
            stmt = stmt.order_by(MemoryEntry.updated_at.desc()).limit(max(1, min(limit, 200)))
            return [self._to_dict(e) for e in session.scalars(stmt)]

    def get(self, owner_id: str, entry_id: str) -> dict[str, Any] | None:
        with self._database.session_factory() as session:
            entry = session.scalar(
                select(MemoryEntry).where(
                    MemoryEntry.id == entry_id,
                    MemoryEntry.owner_id == owner_id,
                )
            )
            return self._to_dict(entry) if entry is not None else None

    def search(self, owner_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Подстрочный поиск по содержанию/заголовку (SQLite/Postgres LIKE)."""
        with self._database.session_factory() as session:
            pattern = f"%{query}%"
            stmt = (
                select(MemoryEntry)
                .where(
                    MemoryEntry.owner_id == owner_id,
                    MemoryEntry.content.ilike(pattern)
                    | MemoryEntry.title.ilike(pattern),
                )
                .order_by(MemoryEntry.updated_at.desc())
                .limit(max(1, min(limit, 100)))
            )
            return [self._to_dict(e) for e in session.scalars(stmt)]

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_dict(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "owner_id": entry.owner_id,
            "kind": entry.kind,
            "title": entry.title,
            "content": entry.content,
            "source": entry.source,
            "task_id": entry.task_id,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
