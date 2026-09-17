"""Async session plumbing for the durable layer.

``expire_on_commit=False`` mirrors the sync layer so objects stay usable after a
commit (an expired attribute would trigger implicit IO, which is not allowed
outside of an await in async SQLAlchemy).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

SessionFactory = async_sessionmaker[AsyncSession]


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Return the async sessionmaker bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def get_session(source: AsyncEngine | SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` for an engine or a prepared factory.

    The session is always closed; nothing is committed implicitly, so a caller
    that raises leaves no half-applied unit of work behind.
    """
    factory = create_session_factory(source) if isinstance(source, AsyncEngine) else source
    session = factory()
    try:
        yield session
    finally:
        await session.close()
