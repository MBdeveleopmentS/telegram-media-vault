from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from vault.config import Settings


def _sqlite_fixups(engine: AsyncEngine) -> None:
    """SQLite (tests only): real SAVEPOINT support and enforced foreign keys."""
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _connect(dbapi_conn, _rec) -> None:
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn) -> None:
        # IMMEDIATE avoids read->write lock-upgrade deadlocks with concurrent writers.
        conn.exec_driver_sql("BEGIN IMMEDIATE")


class Database:
    """Owns the engine and session factory for one process.

    Pool sizes are deliberately small and bounded (threat: DB connection
    exhaustion). Every process has its own pool; total connections are
    roughly sum(pool_size + max_overflow) across processes.
    """

    def __init__(self, url: str, *, pool_size: int = 5, max_overflow: int = 5) -> None:
        kwargs: dict[str, object] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow, pool_timeout=10, pool_recycle=1800)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _sqlite_fixups(self.engine)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, s: Settings) -> Database:
        return cls(s.database_url, pool_size=s.db_pool_size, max_overflow=s.db_max_overflow)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
