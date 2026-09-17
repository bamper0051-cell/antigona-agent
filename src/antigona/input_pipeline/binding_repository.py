"""Repository for TelegramMessageBinding — persistent message-to-task bindings.

Uses SQLAlchemy via the project's ``Database`` session factory.
All methods are async; the repository owns its session lifecycle.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

from antigona.database import Database
from antigona.models import TelegramMessageBinding

logger = logging.getLogger(__name__)


class BindingRepository:
    """Data-access layer for ``TelegramMessageBinding`` rows.

    Uses a ``Database`` instance to obtain sessions internally.
    Thread/request safety: each public method opens and closes its own
    session via ``Database.session()``.

    Usage::

        repo = BindingRepository(database)
        binding = await repo.save(chat_id=..., telegram_message_id=..., ...)
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ── Write ─────────────────────────────────────────────────────────────────

    async def save(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        user_id: int | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        step_id: str | None = None,
        correlation_id: str | None = None,
        message_role: str = "user",
        message_kind: str = "text",
        source_message_id: int | None = None,
        original_text: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> TelegramMessageBinding:
        """Insert or upsert a binding row."""
        for session in self._db.session():
            binding = TelegramMessageBinding(
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                user_id=user_id,
                task_id=task_id,
                session_id=session_id,
                step_id=step_id,
                correlation_id=correlation_id,
                message_role=message_role,
                message_kind=message_kind,
                source_message_id=source_message_id,
                original_text=original_text,
                metadata_json=metadata_json or {},
            )
            session.add(binding)
            session.flush()
            # COMMIT POINT: Database.session() yields a session inside a
            # sessionmaker context that only *closes* (rolls back) on exit, so
            # a flush without an explicit commit is silently discarded — the
            # durable binding never lands. Commit before returning.
            session.commit()
            logger.debug(
                "Binding saved: chat=%d msg=%d task=%s role=%s kind=%s",
                chat_id,
                telegram_message_id,
                task_id or "-",
                message_role,
                message_kind,
            )
            return binding
        raise RuntimeError("Database session not available")

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_message_id(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> TelegramMessageBinding | None:
        """Return the binding for a specific (chat, message), or ``None``."""
        for session in self._db.session():
            stmt = select(TelegramMessageBinding).where(
                TelegramMessageBinding.chat_id == chat_id,
                TelegramMessageBinding.telegram_message_id == telegram_message_id,
            )
            return session.scalar(stmt)
        return None

    async def get_by_task_id(
        self,
        task_id: str,
        *,
        limit: int = 50,
    ) -> list[TelegramMessageBinding]:
        """Return all bindings for a Gateway task/flow, newest first."""
        for session in self._db.session():
            stmt = (
                select(TelegramMessageBinding)
                .where(TelegramMessageBinding.task_id == task_id)
                .order_by(TelegramMessageBinding.created_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt).all())
        return []

    async def get_latest_by_chat(
        self,
        chat_id: int,
        *,
        limit: int = 10,
    ) -> list[TelegramMessageBinding]:
        """Return the most recent bindings for a chat."""
        for session in self._db.session():
            stmt = (
                select(TelegramMessageBinding)
                .where(TelegramMessageBinding.chat_id == chat_id)
                .order_by(TelegramMessageBinding.created_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt).all())
        return []

    # ── Bulk read (restart recovery) ──────────────────────────────────────

    async def load_all_bindings(
        self,
        *,
        limit: int = 500,
    ) -> list[TelegramMessageBinding]:
        """Load most recent bindings across all chats.

        Useful for warming the in-memory cache after a restart.
        Ordered by ``created_at`` descending.
        """
        for session in self._db.session():
            stmt = (
                select(TelegramMessageBinding)
                .order_by(TelegramMessageBinding.created_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt).all())
        return []

    async def load_chat_bindings(
        self,
        chat_id: int,
        *,
        limit: int = 20,
    ) -> list[TelegramMessageBinding]:
        """Load most recent bindings for a specific chat.

        Useful for warming the per-chat context after restart.
        """
        for session in self._db.session():
            stmt = (
                select(TelegramMessageBinding)
                .where(TelegramMessageBinding.chat_id == chat_id)
                .order_by(TelegramMessageBinding.created_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt).all())
        return []

    # ── Edit ──────────────────────────────────────────────────────────────────

    async def update_edited(
        self,
        chat_id: int,
        telegram_message_id: int,
        edited_text: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> TelegramMessageBinding | None:
        """Record an edit on an existing binding.

        Increments ``edit_version`` and sets ``edited_text``.
        Returns the updated binding or ``None`` if no row matched.
        """
        for session in self._db.session():
            values = {
                "edited_text": edited_text,
                "edit_version": TelegramMessageBinding.edit_version + 1,
            }
            if metadata_json is not None:
                values["metadata_json"] = metadata_json

            stmt = (
                update(TelegramMessageBinding)
                .where(
                    TelegramMessageBinding.chat_id == chat_id,
                    TelegramMessageBinding.telegram_message_id == telegram_message_id,
                )
                .values(**values)
                .returning(TelegramMessageBinding)
            )
            row = session.scalar(stmt)
            session.flush()
            # COMMIT POINT: Database.session() yields a session inside a
            # sessionmaker context that only *closes* (rolls back) on exit, so
            # a flush without an explicit commit is silently discarded — the
            # durable binding never lands. Commit before returning.
            session.commit()
            if row is not None:
                logger.debug(
                    "Binding edited: chat=%d msg=%d (v%d)",
                    chat_id,
                    telegram_message_id,
                    row.edit_version,
                )
            return row
        return None

    async def set_task_id(
        self,
        chat_id: int,
        telegram_message_id: int,
        task_id: str,
    ) -> TelegramMessageBinding | None:
        """Associate a binding with a task ID (e.g. after Gateway creates a flow)."""
        return await self._update_field(
            chat_id,
            telegram_message_id,
            {"task_id": task_id},
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _update_field(
        self,
        chat_id: int,
        telegram_message_id: int,
        values: dict[str, Any],
    ) -> TelegramMessageBinding | None:
        for session in self._db.session():
            stmt = (
                update(TelegramMessageBinding)
                .where(
                    TelegramMessageBinding.chat_id == chat_id,
                    TelegramMessageBinding.telegram_message_id == telegram_message_id,
                )
                .values(**values)
                .returning(TelegramMessageBinding)
            )
            result = session.scalar(stmt)
            session.flush()
            # COMMIT POINT: Database.session() yields a session inside a
            # sessionmaker context that only *closes* (rolls back) on exit, so
            # a flush without an explicit commit is silently discarded — the
            # durable binding never lands. Commit before returning.
            session.commit()
            return result
        return None
