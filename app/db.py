from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models (tables defined in app/models/orm.py)."""


def _create_engine() -> AsyncEngine:
    engine = create_async_engine(get_settings().database_url, future=True)

    # SQLite does not enforce foreign keys unless asked, per connection.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


engine: AsyncEngine = _create_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session."""
    async with SessionLocal() as session:
        yield session
