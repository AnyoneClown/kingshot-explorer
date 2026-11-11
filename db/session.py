"""Database session management and configuration."""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base

logger = logging.getLogger(__name__)

T = TypeVar("T")

# CockroachDB reports transaction contention as SQLSTATE 40001 and expects the
# client to retry the whole transaction.
_RETRYABLE_SQLSTATES = {"40001"}
_TRANSACTION_ISOLATION_LEVELS = {"SERIALIZABLE", "REPEATABLE READ", "READ COMMITTED"}
# Generous next to the bot's queries (small rows, at most a few thousand), short next to a
# dead TCP connection.
DB_CONNECT_TIMEOUT_SECONDS = 15
DB_COMMAND_TIMEOUT_SECONDS = 60


def is_retryable_db_error(error: BaseException) -> bool:
    """Return True for CockroachDB serialization failures that should be retried."""
    if not isinstance(error, DBAPIError):
        return False
    candidates = [error.orig, getattr(error.orig, "__cause__", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code in _RETRYABLE_SQLSTATES:
            return True
    message = str(error).lower()
    return "restart transaction" in message or "40001" in message


class DatabaseManager:
    """Manages database connections and sessions."""

    def __init__(self, database_url: str):
        """
        Initialize database manager.

        Args:
            database_url: Database connection URL
        """
        self.database_url = database_url

        # asyncpg waits up to 60 s to connect and has no statement timeout, so a connection
        # left half-open by a Wi-Fi or CockroachDB Cloud blip could stall a query (and the
        # scheduler or gift-code loop awaiting it) until the kernel gives up, ~15 minutes.
        connect_args = (
            {"timeout": DB_CONNECT_TIMEOUT_SECONDS, "command_timeout": DB_COMMAND_TIMEOUT_SECONDS}
            if "+asyncpg" in database_url.split("://", 1)[0]
            else {}
        )

        # Create async engine for CockroachDB with asyncpg
        self.engine = create_async_engine(
            database_url,
            echo=False,  # Set to True for SQL query logging
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,  # Verify connections before using
            pool_recycle=3600,  # Recycle connections after 1 hour
            connect_args=connect_args,
        )

        # Create session factory
        self.async_session_maker = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

        logger.info("DatabaseManager initialized")

    async def create_tables(self):
        """Create all tables in the database."""
        logger.info("Creating database tables...")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created successfully")

    async def drop_tables(self):
        """Drop all tables in the database (use with caution!)."""
        logger.warning("Dropping all database tables...")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        logger.info("Database tables dropped")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Context manager for database sessions.

        Yields:
            AsyncSession: Database session
        """
        async with self.async_session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"Database session error: {e}")
                raise
            finally:
                await session.close()

    async def run_in_transaction(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
        *,
        attempts: int = 4,
        retry_on_integrity_error: bool = False,
        isolation_level: str | None = None,
    ) -> T:
        """Run ``operation`` in its own transaction, retrying CockroachDB contention.

        ``operation`` receives a fresh session for every attempt and must not keep
        state between attempts. Set ``retry_on_integrity_error`` for read-then-insert
        upserts, where a concurrent insert of the same key surfaces as a unique
        violation and the retry then finds the existing row.

        SQLAlchemy's asyncpg driver opens every transaction as READ COMMITTED, and
        CockroachDB honours that once ``sql.txn.read_committed_isolation.enabled`` is
        on. Pass ``isolation_level="SERIALIZABLE"`` when the operation checks a count
        or other invariant before writing, so concurrent callers get a 40001 retry
        instead of both acting on the same stale read.

        The level is set with ``SET TRANSACTION`` as the first statement rather than
        through ``execution_options``: the cockroachdb dialect rejects SQLAlchemy's
        reset to READ COMMITTED when the connection goes back to the pool.
        """
        isolation_statement = None
        if isolation_level is not None:
            level = isolation_level.replace("_", " ").upper()
            if level not in _TRANSACTION_ISOLATION_LEVELS:
                raise ValueError(f"Unsupported isolation level: {isolation_level!r}")
            isolation_statement = text(f"SET TRANSACTION ISOLATION LEVEL {level}")
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self.session() as session:
                    if isolation_statement is not None:
                        await session.execute(isolation_statement)
                    return await operation(session)
            except IntegrityError as error:
                if not retry_on_integrity_error or attempt == attempts:
                    raise
                last_error = error
            except DBAPIError as error:
                if not is_retryable_db_error(error) or attempt == attempts:
                    raise
                last_error = error
            delay = min(2.0, 0.05 * (2 ** (attempt - 1))) * (0.5 + random.random())
            logger.warning(
                "Retrying database transaction (attempt %s/%s) after: %s",
                attempt + 1,
                attempts,
                last_error,
            )
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable") from last_error

    async def close(self):
        """Close the database connection pool."""
        logger.info("Closing database connection pool...")
        await self.engine.dispose()
        logger.info("Database connection pool closed")


# Global database manager instance
_db_manager: DatabaseManager | None = None


def init_db(database_url: str) -> DatabaseManager:
    """
    Initialize the global database manager.

    Args:
        database_url: Database connection URL

    Returns:
        DatabaseManager instance
    """
    global _db_manager
    _db_manager = DatabaseManager(database_url)
    return _db_manager


def get_db() -> DatabaseManager:
    """
    Get the global database manager instance.

    Returns:
        DatabaseManager instance

    Raises:
        RuntimeError: If database manager is not initialized
    """
    if _db_manager is None:
        raise RuntimeError("Database manager not initialized. Call init_db() first.")
    return _db_manager


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injection helper for getting database sessions.

    Yields:
        AsyncSession: Database session
    """
    db = get_db()
    async with db.session() as session:
        yield session
